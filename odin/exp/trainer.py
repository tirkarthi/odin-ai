import glob
import inspect
import os
import pickle
import sys
import tempfile
import warnings
from collections import defaultdict
from functools import partial
from numbers import Number
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Text, Tuple, Union

import numpy as np
import tensorflow as tf
from odin.utils import as_tuple
from six import string_types
from tensorflow import Tensor, Variable
from tensorflow.python import keras
from tensorflow.python.data.ops.dataset_ops import DatasetV2
from tensorflow.python.data.ops.iterator_ops import OwnedIterator
from tensorflow.python.eager.def_function import Function
from tensorflow.python.keras import Model, Sequential
from tensorflow.python.keras.optimizer_v2.optimizer_v2 import OptimizerV2
from tensorflow.python.ops import summary_ops_v2
from tensorflow.python.summary.summary_iterator import summary_iterator
from tqdm import tqdm

__all__ = ['Trainer', 'get_current_trainer']

# ===========================================================================
# Helpers
# ===========================================================================
_BEST_WEIGHTS = {}
_BEST_OPTIMIZER = {}
_CHECKPOINT_MANAGER = {}
_CURRENT_TRAINER = None


def _validate_optimize(func):
  assert callable(func), "optimize must be callable."
  if isinstance(func, Function):
    args = func.function_spec.arg_names
  else:
    spec = inspect.getfullargspec(func)
    args = spec.args + spec.kwonlyargs
  template = ['training', 'n_iter']
  args = args[2:] if 'self' == args[0] else args[1:]
  # assert all(a in args for a in template), \
  #   "optimize function must has the following arguments: %s; but given: %s"\
  #     % (template, args)
  return args


def _is_text(x):
  return isinstance(x, string_types) or \
      (isinstance(x, np.ndarray) and isinstance(x[0], string_types))


def _save_summary(loss, metrics, prefix="", flush=False):
  tf.summary.scalar(f"{prefix}loss", loss)
  for k, v in metrics.items():
    k = f"{prefix}{k}"
    if _is_text(v):
      tf.summary.text(k, v)
    else:
      tf.summary.scalar(k, v)
  if flush:
    tf.summary.flush()


def _print_summary(progress: tqdm,
                   log_tag: str,
                   loss: float,
                   metrics: dict,
                   n_iter: int,
                   is_valid: bool = False):
  log_tag = ("" if len(log_tag) == 0 else f"{log_tag} ")
  if is_valid:
    log_tag = f" * [VALID]{log_tag}"
  progress.write(f"{log_tag} #{int(n_iter)} loss:{loss:.4f}")
  for k, v in metrics.items():
    v = str(v) if _is_text(v) else f"{v:.4f}"
    progress.write(f"{len(log_tag) * ' '} {k}:{v}")


def _process_callback_returns(progress: tqdm,
                              log_tag: str,
                              n_iter: int,
                              metrics: Optional[dict] = None):
  if metrics is not None and isinstance(metrics, dict) and len(metrics) > 0:
    progress.write(f"{log_tag} [Callback#{int(n_iter)}]:")
    for k, v in metrics.items():
      # text
      if _is_text(v):
        tf.summary.text(k, v)
      # scalar or tensor
      else:
        tf.cond(tf.less(tf.rank(v), 1),
                true_fn=lambda: tf.summary.scalar(k, v),
                false_fn=lambda: tf.summary.histogram(k, v))
      progress.write(f" {k}:{v}")


def read_tensorboard(logdir: str) -> Dict[Text, Tuple[float, int, float]]:
  r""" Read Tensorboard event files from a `logdir`

  Return:
    a dictionary mapping from `tag` (string) to list of tuple
    `(wall_time, step, value)`
  """
  all_log = defaultdict(list)
  for f in sorted(glob.glob(f"{logdir}/event*"),
                  key=lambda x: int(os.path.basename(x).split('.')[3])):
    for event in summary_iterator(f):
      t = event.wall_time
      step = event.step
      summary = event.summary
      for value in event.summary.value:
        tag = value.tag
        meta = value.metadata
        dtype = meta.plugin_data.plugin_name
        data = tf.make_ndarray(value.tensor)
        if dtype == "scalars":
          pass
        elif dtype == "text":
          if len(value.tensor.tensor_shape.dim) == 0:
            data = str(data.tolist(), 'utf-8')
          else:
            data = np.array([str(i, 'utf-8') for i in data])
        else:
          raise NotImplementedError(f"Unknown data type: {summary}")
        all_log[tag].append((t, step, data))
  all_log = {i: sorted(j, key=lambda x: x[1]) for i, j in all_log.items()}
  return all_log


# ===========================================================================
# Main
# ===========================================================================
class Trainer(object):
  r""" Simple training procedure """

  @staticmethod
  def save_weights(models, optimizers=None):
    r""" Fast store the weights of models in-memory, only the lastest
    version of the weights are kept """
    for i in tf.nest.flatten(models):
      if not isinstance(i, keras.layers.Layer):
        continue
      _BEST_WEIGHTS[id(i)] = i.get_weights()
    if optimizers is not None:
      for i in tf.nest.flatten(optimizers):
        if isinstance(i, tf.optimizers.Optimizer):
          _BEST_OPTIMIZER[id(i)] = i.get_weights()

  @staticmethod
  def restore_weights(models, optimizers=None):
    r""" Fast recover the weights of models, only the lastest version
    of the weights are kept """
    for i in tf.nest.flatten(models):
      if not isinstance(i, keras.layers.Layer):
        continue
      if id(i) in _BEST_WEIGHTS:
        i.set_weights(_BEST_WEIGHTS[id(i)])
    if optimizers is not None:
      for i in tf.nest.flatten(optimizers):
        if isinstance(i, tf.optimizers.Optimizer):
          if id(i) in _BEST_OPTIMIZER:
            i.set_weights(_BEST_OPTIMIZER[id(i)])

  @staticmethod
  def save_checkpoint(dir_path,
                      models,
                      optimizers=None,
                      trainer=None,
                      max_to_keep=5):
    r""" Save checkpoint """
    if optimizers is None:
      optimizers = []
    optimizers = tf.nest.flatten(optimizers)
    assert all(isinstance(opt, tf.optimizers.Optimizer) for opt in optimizers), \
      "optimizer must be instance of tf.optimizers.Optimizer"
    dir_path = os.path.abspath(dir_path)
    if not os.path.exists(dir_path):
      os.mkdir(dir_path)
    elif os.path.isfile(dir_path):
      raise ValueError("dir_path must be path to a folder")
    models = [
        i for i in tf.nest.flatten(models)
        if isinstance(i, (tf.Variable, keras.layers.Layer))
    ]
    footprint = dir_path + \
      ''.join(sorted([str(id(i)) for i in optimizers])) + \
      ''.join(sorted([str(id(i)) for i in models]))
    if footprint in _CHECKPOINT_MANAGER:
      manager, cp = _CHECKPOINT_MANAGER[footprint]
    else:
      models = {"model%d" % idx: m for idx, m in enumerate(models)}
      models.update(
          {"optimizer%d" % idx: m for idx, m in enumerate(optimizers)})
      cp = tf.train.Checkpoint(**models)
      manager = tf.train.CheckpointManager(cp,
                                           directory=dir_path,
                                           max_to_keep=max_to_keep)
      _CHECKPOINT_MANAGER[footprint] = (manager, cp)
    manager.save()
    with open(os.path.join(dir_path, 'optimizers.pkl'), 'wb') as f:
      pickle.dump(
          [(opt.__class__.__name__, opt.get_config()) for opt in optimizers], f)
    with open(os.path.join(dir_path, 'max_to_keep'), 'wb') as f:
      pickle.dump(max_to_keep, f)
    if trainer is not None:
      with open(os.path.join(dir_path, 'trainer.pkl'), 'wb') as f:
        pickle.dump(trainer, f)

  @staticmethod
  def restore_checkpoint(dir_path, models=None, optimizers=None, index=-1):
    r""" Restore saved checkpoint

    Returns:
      models : list of `keras.Model` or `tf.Variable`
      optimizers : list of `tf.optimizers.Optimizer`
      trainer : `odin.backend.Trainer` or `None`
    """
    dir_path = os.path.abspath(dir_path)
    kwargs = {}
    if not os.path.exists(dir_path):
      os.mkdir(dir_path)
    elif os.path.isfile(dir_path):
      raise ValueError("dir_path must be path to a folder")
    # check models and optimizers
    if models is None and optimizers is None:
      footprint = [name for name in _CHECKPOINT_MANAGER \
                   if dir_path in name]
      if len(footprint) == 0:
        raise ValueError("Cannot find checkpoint information for path: %s" %
                         dir_path)
      footprint = footprint[0]
      manager, cp = _CHECKPOINT_MANAGER[footprint]
    # given models
    elif models is not None:
      models = [
          i for i in tf.nest.flatten(models)
          if isinstance(i, (tf.Variable, keras.layers.Layer))
      ]
      models_ids = ''.join(sorted([str(id(i)) for i in models]))
      with open(os.path.join(dir_path, 'max_to_keep'), 'rb') as f:
        max_to_keep = pickle.load(f)
      # not provided optimizers
      if optimizers is None:
        footprint = [name for name in _CHECKPOINT_MANAGER \
                     if dir_path in name and models_ids in name]
        if len(footprint) > 0:
          manager, cp = _CHECKPOINT_MANAGER[footprint[0]]
        else:  # create optimizer
          with open(os.path.join(dir_path, 'optimizers.pkl'), 'rb') as f:
            optimizers = pickle.load(f)
          optimizers = [
              tf.optimizers.get(name).from_config(config)
              for name, config in optimizers
          ]
          kwargs = {"model%d" % idx: m for idx, m in enumerate(models)}
          kwargs.update(
              {"optimizer%d" % idx: m for idx, m in enumerate(optimizers)})
          cp = tf.train.Checkpoint(**kwargs)
          manager = tf.train.CheckpointManager(cp,
                                               directory=dir_path,
                                               max_to_keep=max_to_keep)
      # both models and optimizers available
      else:
        optimizers = tf.nest.flatten(optimizers)
        optimizers_ids = ''.join(sorted([str(id(i)) for i in optimizers]))
        footprint = [name for name in _CHECKPOINT_MANAGER \
                     if dir_path in name and \
                       optimizers_ids in name and \
                         models_ids in name]
        if len(footprint) > 0:
          manager, cp = _CHECKPOINT_MANAGER[footprint[0]]
        else:
          kwargs = {"model%d" % idx: m for idx, m in enumerate(models)}
          kwargs.update(
              {"optimizer%d" % idx: m for idx, m in enumerate(optimizers)})
          cp = tf.train.Checkpoint(**kwargs)
          manager = tf.train.CheckpointManager(cp,
                                               directory=dir_path,
                                               max_to_keep=max_to_keep)
    # only given optimizers, cannot do anything
    else:
      raise ValueError("Not support for models=%s optimizers=%s" %
                       (str(models), str(optimizers)))
    cp.restore(manager.checkpoints[int(index)])
    # restore trainer (if exist)
    trainer = None
    trainer_path = os.path.join(dir_path, 'trainer.pkl')
    if os.path.exists(trainer_path):
      with open(trainer_path, 'rb') as f:
        trainer = pickle.load(f)
    # return
    models = [getattr(cp, k) for k in sorted(dir(cp)) if 'model' in k[:5]]
    optimizers = [
        getattr(cp, k) for k in sorted(dir(cp)) if 'optimizer' in k[:9]
    ]
    return models, optimizers, trainer

  @staticmethod
  def early_stop(losses,
                 threshold=0.001,
                 progress_length=0,
                 min_epoch=-1,
                 verbose=0):
    r""" Early stopping based on generalization loss and the three rules:

      - Stop when generalization error exceeds threshold in a number of
          successive steps.
      - Stop as soon as the generalization loss exceeds a certain threshold.
      - Surpress stopping if the training is still progress rapidly.

    Generalization loss: `GL(t) = losses[-1] / min(losses[:-1]) - 1`

      - if `GL(t)` <= 0 : save the best model
      - if `GL(t)` <= threshold : continue training
      - if `GL(t)` > threshold : stop training

    Progression: `PG(t) = 10 * sum(L) / (k * min(L))` where `L = losses[-k:]`

    The condition for early stopping: `GL(t) / PG(t) >= threshold`

    Arguments:
      losses : List of loss values (smaller is better)
      threshold : Float. Determine by `generalization_error / progression`
      progress_length : Integer. Number of steps to look into the past for
        estimating the training progression. If smaller than 2, turn-off
        progression for early stopping.
      min_epoch: Minimum number of epoch until early stop kicks in.
        Note, all the metrics won't be updated until the given epoch.

    Return:

    Reference:
      Prechelt, L. (1998). "Early Stopping | but when?".
    """
    if len(losses) < max(2., min_epoch):
      tf.print("[EarlyStop] Priming first 2 warmup epochs.")
      return Trainer.SIGNAL_BEST
    # generalization error (smaller is better)
    current = losses[-1]
    best = np.min(losses[:-1])
    generalization = current / best - 1.  # <0 = improvement
    # progression (bigger is better)
    if progress_length > 1:
      progress = losses[-progress_length:]
      progression = 10 * \
        (np.sum(progress) / (progress_length * np.min(progress)) - 1)
    else:
      progression = 1.
    # thresholding
    error = generalization / progression
    threshold = np.abs(threshold)
    if error >= -threshold:
      tf.print("[EarlyStop] improvement:%.4f progression:%.4f threshold:%.4f" %
               (-generalization, progression, threshold))
      return Trainer.SIGNAL_TERMINATE
    elif generalization < 0:
      if verbose:
        tf.print("[EarlyStop] Best model, improvement:%.4f, threshold:%.4f" %
                 ((best / current - 1.), np.abs(threshold)))
      return Trainer.SIGNAL_BEST

  @staticmethod
  def apply_gradients(tape, optimizer, loss, model_or_weights):
    r"""
    Arguments:
      tape : GradientTape (optional). If not given, no optimization is
        performed.
      optimizer : Instance of `tf.optimizers.Optimizer`.
      loss : a Tensor value with rank 0.
      model_or_weights : List of keras.layers.Layer or tf.Variable, only
        `trainable` variables are optimized.

    Returns:
      gradients : List of gradient Tensor
    """
    if tape is None:
      return
    assert isinstance(tape, tf.GradientTape), "tape must be tf.GradientTape"
    assert isinstance(optimizer, tf.optimizers.Optimizer)
    model_or_weights = tf.nest.flatten(model_or_weights)
    weights = []
    for i in model_or_weights:
      if not i.trainable:
        continue
      if isinstance(i, tf.Variable):
        weights.append(i)
      else:
        weights += i.trainable_variables
    with tape.stop_recording():
      grads = tape.gradient(loss, weights)
      optimizer.apply_gradients(grads_and_vars=zip(grads, weights))
    return grads

  @staticmethod
  def prepare(ds,
              preprocess=None,
              postprocess=None,
              batch_size=128,
              epochs=1,
              cache='',
              drop_remainder=False,
              shuffle=None,
              parallel_preprocess=0,
              parallel_postprocess=0):
    r""" A standarlized procedure for preparing `tf.data.Dataset` for training
    or evaluation.

    Arguments:
      ds : `tf.data.Dataset`
      preprocess : Callable (optional). Call at the beginning when a single
        example is fetched.
      postprocess : Callable (optional). Called after `.batch`, which processing
        the whole minibatch.
      parallel_preprocess, parallel_postprocess : Integer.
        - if value is 0, process sequentially
        - if value is -1, using `tf.data.experimental.AUTOTUNE`
        - if value > 0, explicitly specify the number of process for running
            map task
    """
    parallel_preprocess = None if parallel_preprocess == 0 else \
      (tf.data.experimental.AUTOTUNE if parallel_preprocess == -1 else
       int(parallel_preprocess))
    parallel_postprocess = None if parallel_postprocess == 0 else \
      (tf.data.experimental.AUTOTUNE if parallel_postprocess == -1 else
       int(parallel_postprocess))

    if not isinstance(ds, DatasetV2):
      ds = tf.data.Dataset.from_tensor_slices(ds)

    if shuffle:
      if isinstance(shuffle, Number):
        ds = ds.shuffle(buffer_size=int(shuffle), reshuffle_each_iteration=True)
      else:
        ds = ds.shuffle(buffer_size=10000, reshuffle_each_iteration=True)

    if preprocess is not None:
      ds = ds.map(preprocess, num_parallel_calls=parallel_preprocess)
      if cache is not None:
        ds = ds.cache(cache)
    ds = ds.repeat(epochs).batch(batch_size, drop_remainder=drop_remainder)

    if postprocess is not None:
      ds = ds.map(postprocess, num_parallel_calls=parallel_postprocess)
    ds = ds.prefetch(tf.data.experimental.AUTOTUNE)
    return ds

  #######################################################
  def __init__(self, logdir: Optional[str] = None, trace_on: bool = False):
    super().__init__()
    if logdir is None:
      logdir = tempfile.mkdtemp()
    logdir = os.path.abspath(os.path.expanduser(logdir))
    self.logdir = logdir
    self.trace_on = bool(trace_on)
    self.n_iter = 0
    # default attributes
    self._summary_writer = None
    self._current_valid_loss = []
    self._current_train_progress = None
    self._cached_tensorboard = None
    self._is_training = False

  @property
  def tensorboard(self) -> Dict[Text, Tuple[float, int, float]]:
    r""" Return stored data from tensorboard """
    if self._cached_tensorboard is None:
      if not os.path.exists(self.logdir):
        self._cached_tensorboard = dict()
      else:
        self._cached_tensorboard = read_tensorboard(self.logdir)
    return self._cached_tensorboard

  @property
  def train_loss(self) -> List[float]:
    losses = self.tensorboard.get('train_loss', [])
    losses = [i[-1] for i in losses]
    return losses

  @property
  def valid_loss(self) -> List[float]:
    losses = self.tensorboard.get('valid_loss', [])
    losses = [i[-1] for i in losses]
    return losses

  @property
  def train_metrics(self) -> Dict[str, List[float]]:
    metrics = dict()
    for key, val in self.tensorboard.items():
      if "train/" == key[:6] and key != "train_loss":
        key = key[6:]
        val = [i[-1] for i in val]
        metrics[key] = val
    return metrics

  @property
  def valid_metrics(self) -> Dict[str, List[float]]:
    metrics = dict()
    for key, val in self.tensorboard.items():
      if "valid/" == key[:6] and key != "valid_loss":
        key = key[6:]
        val = [i[-1] for i in val]
        metrics[key] = val
    return metrics

  @property
  def current_valid_loss(self) -> List[float]:
    return self._current_valid_loss

  @property
  def summary_writer(self) -> tf.summary.SummaryWriter:
    if self._summary_writer is None:
      self._summary_writer = tf.summary.create_file_writer(self.logdir)
    return self._summary_writer

  @property
  def is_training(self) -> bool:
    return self._is_training

  def terminate(self):
    self._is_training = False

  def __getstate__(self):
    return (self.logdir, self.trace_on, self.n_iter)

  def __setstate__(self, states):
    (self.logdir, self.trace_on, self.n_iter) = states
    # default attributes
    self._summary_writer = None
    self._current_valid_loss = []
    self._current_train_progress = None
    self._cached_tensorboard = None
    self._is_training = False

  def fit(self,
          train_ds: DatasetV2,
          optimize: Callable[..., Tuple[Tensor, Dict[str, Tensor]]],
          valid_ds: Optional[DatasetV2] = None,
          valid_freq: int = 1000,
          valid_interval: float = 0,
          compile_graph: bool = True,
          autograph: bool = True,
          logging_interval: float = 3,
          log_tag: str = '',
          max_iter: int = -1,
          terminate_on_nan: bool = True,
          callback: Callable[[], Optional[dict]] = lambda: None):
    r""" A simplified fitting API

    Arguments:
      train_ds : tf.data.Dataset. Training dataset.
      optimize : Callable. Optimization function, return loss and a list of
        metrics. The input arguments must be:
          - ('inputs', 'tape', 'training', 'n_iter');
        and the function must returns:
          - ('loss': `tf.Tensor`, 'metrics': `dict(str, tf.Tensor)`)
      valid_ds : tf.data.Dataset. Validation dataset
      valid_freq : an Integer. The frequency of validation task, based on
        the current number of iteration.
      valid_interval : a Scalar. The number of second until next validation.
      autograph : Boolean. Enable static graph for the `optimize` function.
      logging_interval : Scalar. Interval for print out log information
        (in second).
      max_iter : An Interger or `None`. Maximum number of iteration for
        training. If `max_iter <= 0`, iterate the training data until the end.
      callback : Callable take no input arguments.
        The callback will be called after every fixed number of iteration
        according to `valid_freq`, or fixed duration defined by `valid_interval`

    Example:
      def optimize(inputs, tape, n_iter, training):
        return loss, dict(llk=0, div=0, elbo=0)
    """
    if isinstance(optimize, partial):
      func_name = optimize.func
    elif isinstance(optimize, Function):
      func_name = optimize._python_function
    else:
      func_name = optimize
    if inspect.ismethod(func_name):
      func_obj = func_name.__self__
    else:
      func_obj = None
    func_name = func_name.__name__
    # reset last stored valid losses
    self._current_valid_loss = []
    if len(log_tag) > 0:
      log_tag += " "
    if hasattr(train_ds, '__len__') and max_iter <= 0:
      max_iter = len(train_ds)
    ### Prepare the data
    assert isinstance(train_ds, (tf.data.Dataset, OwnedIterator)), \
      'train_ds must be instance of tf.data.Datasets'
    if valid_ds is not None:
      assert isinstance(valid_ds, (tf.data.Dataset, OwnedIterator)), \
        'valid_ds must be instance of tf.data.Datasets'
    valid_freq = max(1, int(valid_freq))
    valid_interval = float(valid_interval)
    if valid_interval > 0:  # prefer the interval
      valid_freq = 1
    ### create autograph version of optimize
    optimize_args = _validate_optimize(optimize)
    if compile_graph and not isinstance(optimize, Function):
      optimize = tf.function(optimize,
                             autograph=bool(autograph),
                             experimental_compile=None)

    ### helper function for training iteration
    def fn_step(n_iter, inputs, training):
      kw = dict()
      if 'n_iter' in optimize_args:
        kw['n_iter'] = n_iter
      if 'training' in optimize_args:
        kw['training'] = training
      if isinstance(inputs, dict):
        kw.update(inputs)
        loss, metrics = optimize(**kw)
      else:
        loss, metrics = optimize(inputs, **kw)
      assert isinstance(metrics, dict), "Metrics must be instance of dictionary"
      return loss, metrics

    ### validation
    def valid():
      epoch_loss = []
      epoch_metrics = defaultdict(list)
      valid_progress = tqdm(
          enumerate(valid_ds.repeat(1)),
          desc=f"Validating {valid_freq}(it) or {valid_interval:.1f}(s)")
      for it, inputs in valid_progress:
        _loss, _metrics = fn_step(it, inputs, training=False)
        # store for calculating average
        epoch_loss.append(_loss)
        for k, v in _metrics.items():
          epoch_metrics[k].append(v)
      epoch_loss = tf.reduce_mean(epoch_loss, axis=0)
      epoch_metrics = {
          k: tf.reduce_mean(v, axis=0) for k, v in epoch_metrics.items()
      }
      return epoch_loss, epoch_metrics

    def train():
      global _CURRENT_TRAINER
      _CURRENT_TRAINER = self
      self._is_training = True
      progress = tqdm(train_ds, desc=f"Traning {max_iter}(its)")
      self._current_train_progress = progress
      start_time = progress.start_t
      last_print_time = 0
      last_valid_time = start_time
      for cur_iter, inputs in enumerate(progress):
        self.n_iter += 1
        # ====== check maximum iteration ====== #
        if max_iter > 0 and cur_iter >= max_iter:
          break
        tf.summary.experimental.set_step(self.n_iter)
        # the tensorboard will change after each iteration
        self._cached_tensorboard = None
        # ====== train ====== #
        loss, metrics = fn_step(self.n_iter, inputs, training=True)
        # do not record the loss and metrics at every iteration, the
        # performance will drop about 40%
        if terminate_on_nan and np.isnan(loss) or np.isinf(loss):
          progress.write(
              f" *Terminated on NaN loss at iteration #{int(self.n_iter)}")
          for k, v in metrics.items():
            progress.write(f"\t{k}: {v}")
          break
        # ====== logging ====== #
        interval = progress._time() - last_print_time
        if interval >= logging_interval:
          # summarize the batch loss and metrics
          _save_summary(loss, metrics, prefix="train/")
          _print_summary(progress,
                         log_tag,
                         loss,
                         metrics,
                         self.n_iter,
                         is_valid=False)
          last_print_time = progress._time()
        # ====== validation ====== #
        interval = progress._time() - last_valid_time
        if self.n_iter % valid_freq == 0 and interval >= valid_interval:
          if valid_ds is not None:
            # finish the validation
            val_loss, val_metrics = valid()
            self._current_valid_loss.append(val_loss)
            _save_summary(val_loss, val_metrics, prefix="valid/", flush=True)
            _print_summary(progress,
                           log_tag,
                           val_loss,
                           val_metrics,
                           self.n_iter,
                           is_valid=True)
          # callback always called
          _process_callback_returns(progress, log_tag, self.n_iter, callback())
          if not self.is_training:
            break
          last_valid_time = progress._time()
        #########
      # Final callback to signal train ended
      _process_callback_returns(progress, log_tag, self.n_iter, callback())
      # end the progress
      progress.clear()
      progress.close()

    ### train and return
    with self.summary_writer.as_default():
      if self.trace_on:
        tf.summary.trace_on(graph=True, profiler=False)
      else:
        tf.summary.trace_off()
      train()
      if self.trace_on:
        tf.summary.trace_export(name=func_name,
                                step=0,
                                profiler_outdir=self.logdir)
        tf.summary.trace_off()
    if isinstance(func_obj, (Model, Sequential)):
      self.write_keras_graph(func_obj, name=func_obj.__class__.__name__)
    self.summary_writer.flush()
    self._current_train_progress = None
    self._current_valid_loss = []
    return self

  def print(self, msg: str):
    r""" Print log message without interfering the current `tqdm` progress bar """
    if self._current_train_progress is None:
      print(msg)
    else:
      self._current_train_progress.write(msg)
    return self

  def write_keras_graph(self,
                        model: Union[Model, Sequential],
                        step: int = 0,
                        name: str = "keras"):
    r""" Writes Keras graph networks to TensorBoard. """
    with self.summary_writer.as_default():
      with summary_ops_v2.always_record_summaries():
        if not model.run_eagerly:
          summary_ops_v2.graph(keras.backend.get_graph(), step=step)
        summary_writable = (model._is_graph_network or
                            model.__class__.__name__ == 'Sequential')
        if summary_writable:
          summary_ops_v2.keras_model(name=str(name), data=model, step=step)
    return self

  def plot_learning_curves(self,
                           path: str = "/tmp/tmp.png",
                           summary_steps: Tuple[int, int] = [10, 5],
                           show_validation: bool = True,
                           dpi: int = 200,
                           title: Optional[str] = None):
    r""" Plot learning curves

    Arguments:
      path: save path for the figure
      summary_steps: a tuple of Integer.
        Number of iteration for estimating the mean and variance for training
        and validation.
    """
    import seaborn as sns
    from matplotlib import pyplot as plt
    from odin import visual as vs
    sns.set()
    summary_steps = as_tuple(summary_steps, N=2, t=int)
    is_validated = bool(len(self.valid_loss) > 0) and bool(show_validation)
    n_metrics = len(self.train_metrics)
    metrics_name = list(self.train_metrics.keys())
    metrics_name = ["loss"] + tf.nest.flatten(metrics_name)
    # create the figure
    ncol = 2 if is_validated else 1
    nrow = 1 + n_metrics
    fig = plt.figure(figsize=(8, nrow * 3))
    # prepare the results
    train = [self.train_loss] + \
      [self.train_metrics[i] for i in metrics_name[1:]]
    train_name = metrics_name
    if is_validated:
      valid = [self.valid_loss] + \
        [self.valid_metrics[i] for i in metrics_name[1:]]
      valid_name = ['val_' + i for i in metrics_name]
      all_data = zip([i for pair in zip(train_name, valid_name) for i in pair],
                     [i for pair in zip(train, valid) for i in pair])
    else:
      all_data = zip(train_name, train)
    # plotting
    subplots = []
    for idx, (name, data) in enumerate(all_data):
      ax = plt.subplot(nrow, ncol, idx + 1)
      subplots.append(ax)
      batch_size = summary_steps[1 if 'val_' == name[:4] else 0]
      if batch_size > len(data):
        warnings.warn(
            "Given summary_steps=%d but only has %d data points, skip plot!" %
            (batch_size, len(data)))
        return self
      data = [batch for batch in tf.data.Dataset.from_tensor_slices(\
        data).batch(batch_size)]
      data_avg = np.array([np.mean(i) for i in data])
      data_std = np.array([np.std(i) for i in data])
      data_min = np.min(data_avg)
      data_max = np.max(data_avg)
      plt.plot(data_avg, label='Avg.')
      plt.plot(np.argmin(data_avg),
               data_min,
               marker='o',
               color='green',
               alpha=0.5,
               label='Min:%.2f' % data_min)
      plt.plot(np.argmax(data_avg),
               data_max,
               marker='o',
               color='red',
               alpha=0.5,
               label='Max:%.2f' % data_max)
      plt.fill_between(np.arange(len(data_avg)),
                       data_avg + data_std,
                       data_avg - data_std,
                       alpha=0.3)
      plt.title(name)
      plt.legend(fontsize=10)
    # set the xlabels
    for ax, step in zip(subplots[-2:], summary_steps):
      ax.set_xlabel("#Iter * %d" % step)
    if title is not None:
      fig.suptitle(str(title))
      fig.tight_layout(rect=[0, 0.03, 1, 0.97])
    else:
      fig.tight_layout()
    if path is None:
      return fig
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return self


# ===========================================================================
# Helpers
# ===========================================================================
def get_current_trainer() -> Trainer:
  return _CURRENT_TRAINER
