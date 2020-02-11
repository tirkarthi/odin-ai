import inspect
import itertools
import logging
import os
import re
import shutil
import sqlite3
import sys
import warnings
from contextlib import contextmanager
from copy import deepcopy
from numbers import Number

from hydra._internal.config_loader import ConfigLoader
from hydra._internal.core_plugins import BasicLauncher
from hydra._internal.hydra import Hydra, HydraConfig
from hydra._internal.pathlib import Path
from hydra._internal.utils import (create_config_search_path, get_args_parser,
                                   run_hydra)
from hydra.plugins.common.utils import (configure_log, filter_overrides,
                                        run_job, setup_globals,
                                        split_config_path)
from omegaconf import DictConfig, OmegaConf, open_dict
from six import string_types

from odin.utils import as_tuple, get_formatted_datetime
from odin.utils.crypto import md5_checksum, md5_folder
from odin.utils.mpi import MPI

# ===========================================================================
# Helpers
# ===========================================================================
LOGGER = logging.getLogger("Experimenter")


def _abspath(path):
  if '$' in path:
    if not re.search(r"\$\{\w+\}", path):
      raise ValueError("Wrong specification for env variable %s, use ${.}" %
                       path)
    path = os.path.expandvars(path)
  if '$' in path:
    raise ValueError("Invalid path '%s', empty env variable" % path)
  path = os.path.abspath(os.path.expanduser(path))
  return path


def _overrides(overrides):
  if isinstance(overrides, dict):
    overrides = [
        "%s=%s" % (str(key), \
          ','.join([str(v) for v in value])
                   if isinstance(value, (tuple, list)) else str(value))
        for key, value in overrides.items()
    ]
  elif isinstance(overrides, string_types):
    overrides = [overrides]
  elif overrides is None:
    overrides = []
  return list(overrides)


def _all_keys(d, base):
  keys = []
  for k, v in d.items():
    if len(base) > 0:
      k = base + "." + k
    keys.append(k)
    if hasattr(v, 'items'):
      keys += _all_keys(v, k)
  return keys


# ===========================================================================
# Hydra Launcher
# ===========================================================================
class ParallelLauncher(BasicLauncher):
  r"""Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

  This launcher won't run parallel task if `ncpu<=1`
  """

  def __init__(self, ncpu=1):
    super().__init__()
    self.ncpu = ncpu

  def launch(self, job_overrides):
    setup_globals()
    configure_log(self.config.hydra.hydra_logging, self.config.hydra.verbose)
    sweep_dir = self.config.hydra.sweep.dir
    Path(str(sweep_dir)).mkdir(parents=True, exist_ok=True)
    LOGGER.info("Launching {} jobs locally".format(len(job_overrides)))

    def run_task(job):
      idx, overrides = job
      LOGGER.info("\t#{} : {}".format(idx,
                                      " ".join(filter_overrides(overrides))))
      sweep_config = self.config_loader.load_sweep_config(
          self.config, list(overrides))
      with open_dict(sweep_config):
        # id is concatenated overrides here
        sweep_config.hydra.job.id = '_'.join(sorted(overrides))
        sweep_config.hydra.job.num = idx
      HydraConfig().set_config(sweep_config)
      ret = run_job(
          config=sweep_config,
          task_function=self.task_function,
          job_dir_key="hydra.sweep.dir",
          job_subdir_key="hydra.sweep.subdir",
      )
      configure_log(self.config.hydra.hydra_logging, self.config.hydra.verbose)
      return (idx, ret)

    if self.ncpu > 1:
      jobs = list(enumerate(job_overrides))
      runs = sorted([
          ret
          for ret in MPI(jobs=jobs, func=run_task, ncpu=int(self.ncpu), batch=1)
      ])
      runs = [i[1] for i in runs]
    else:
      runs = [run_task(job)[1] for job in enumerate(job_overrides)]
    return runs


def _to_sqltype(obj):
  if obj is None:
    return "NULL"
  if isinstance(obj, string_types):
    return "TEXT"
  if isinstance(obj, Number):
    obj = float(obj)
    if obj.is_integer():
      return "INTEGER"
    return "REAL"
  return "BLOB"


# ===========================================================================
# SQLlite
# ===========================================================================
class ExperimentManager():

  def __init__(self, exp):
    assert isinstance(exp, Experimenter), \
      "exp must be instance of Experimenter, but given: %s" % str(type(exp))
    self.path = _abspath(os.path.join(exp._save_path, 'exp.log'))

  def update_status(self):
    pass

  def close(self):
    pass


class SQLExperimentManager(ExperimentManager):

  @property
  def connection(self) -> sqlite3.Connection:
    if not hasattr(self, '_connection'):
      self._connection = sqlite3.connect(self.path,
                                         timeout=self.timeout,
                                         check_same_thread=True)
    return self._connection

  @contextmanager
  def cursor(self) -> sqlite3.Cursor:
    if not hasattr(self, '_cursor'):
      self._cursor = self.connection.cursor()
    yield self._cursor
    self._connection.commit()

  def close(self):
    if hasattr(self, '_connection'):
      if hasattr(self, '_cursor'):
        self._cursor.close()
        del self._cursor
      self._connection.close()
      del self._connection


# ===========================================================================
# Main class
# ===========================================================================
_INSTANCES = {}


class Experimenter():
  r"""
  """

  def __init__(self,
               save_path,
               config_path="",
               ncpu=1,
               exclude_keys=[],
               consistent_model=True):
    # already init, return by singleton
    if hasattr(self, '_configs'):
      return
    self.ncpu = int(ncpu)
    # ====== check save path ====== #
    self._save_path = _abspath(save_path)
    if os.path.isfile(self._save_path):
      raise ValueError("save_path='%s' must be a folder" % self._save_path)
    if not os.path.exists(self._save_path):
      os.mkdir(self._save_path)
    self._manager = ExperimentManager(self)
    # ====== load configs ====== #
    self.config_path = _abspath(config_path)
    assert os.path.isfile(self.config_path), \
      "Config file does not exist: %s" % self.config_path
    search_path = create_config_search_path(os.path.dirname(self.config_path))
    self.config_loader = ConfigLoader(config_search_path=search_path,
                                      default_strict=False)
    self._configs = self.load_configuration()
    self._all_keys = set(_all_keys(self._configs, base=""))
    self._exclude_keys = as_tuple(exclude_keys, t=string_types)
    self.consistent_model = bool(consistent_model)

  ####################### Static helpers
  @staticmethod
  def remove_keys(cfg: DictConfig, keys=[], copy=True) -> DictConfig:
    r""" Remove list of keys from given configs """
    assert isinstance(cfg, (DictConfig, dict))
    exclude_keys = as_tuple(keys, t=string_types)
    if len(exclude_keys) > 0:
      if copy:
        cfg = deepcopy(cfg)
      for key in exclude_keys:
        if '.' in key:
          key = key.split('.')
          attr = cfg
          for i in key[:-1]:
            attr = getattr(attr, i)
          del attr[key[-1]]
        else:
          del cfg[key]
    return cfg

  @staticmethod
  def hash_config(cfg: DictConfig, exclude_keys=[]) -> str:
    r"""
    cfg : dictionary of configuration to generate an unique identity
    exclude_keys : list of string, given keys will be ignored from the
      configuraiton
    """
    assert isinstance(cfg, (DictConfig, dict))
    cfg = Experimenter.remove_keys(cfg, copy=True, keys=exclude_keys)
    return md5_checksum(cfg)[:8]

  @staticmethod
  def match_arguments(func: callable, cfg: DictConfig, ignores=[],
                      **defaults) -> dict:
    kwargs = {}
    spec = inspect.getfullargspec(func)
    args = spec.args
    if 'self' == args[0]:
      args = args[1:]
    for name in args:
      if name in cfg:
        kwargs[name] = cfg[name]
      elif name in defaults:
        kwargs[name] = defaults[name]
    for name in as_tuple(ignores, t=str):
      if name in kwargs:
        del kwargs[name]
    return kwargs

  ####################### Property
  @property
  def exclude_keys(self):
    return self._exclude_keys

  @property
  def manager(self) -> ExperimentManager:
    return self._manager

  @property
  def db_path(self):
    return os.path.join(self._save_path, 'exp.db')

  @property
  def configs(self) -> DictConfig:
    return deepcopy(self._configs)

  ####################### Helpers
  def get_output_path(self, cfg: DictConfig = None):
    if cfg is None:
      cfg = self.configs
    key = Experimenter.hash_config(cfg, self.exclude_keys)
    path = os.path.join(self._save_path, 'exp_%s' % key)
    if not os.path.exists(path):
      os.mkdir(path)
    return path

  def get_model_path(self, cfg: DictConfig = None):
    output_path = self.get_output_path(cfg)
    return os.path.join(output_path, 'model')

  def get_hydra_path(self):
    path = os.path.join(self._save_path, 'hydra')
    if not os.path.exists(path):
      os.mkdir(path)
    return path

  def get_config_path(self, cfg: DictConfig = None, datetime=False):
    output_path = self.get_output_path(cfg)
    if datetime:
      return os.path.join(
          output_path,
          'configs_%s.yaml' % get_formatted_datetime(only_number=False))
    return os.path.join(output_path, 'configs.yaml')

  def parse_overrides(self, overrides=[], **configs):
    overrides = _overrides(overrides) + _overrides(configs)
    lists = []
    for s in overrides:
      key, value = s.split("=")
      assert key in self._all_keys, \
        "Cannot find key='%s' in default configs, possible key are: %s" % \
          (key, ', '.join(self._all_keys))
      lists.append(["{}={}".format(key, val) for val in value.split(",")])
    lists = list(itertools.product(*lists))
    return lists

  def load_configuration(self, overrides=[], **configs):
    overrides = _overrides(overrides) + _overrides(configs)
    # create the config
    returns = []
    for overrides in self.parse_overrides(overrides):
      cfg = self.config_loader.load_configuration(\
        config_file=os.path.basename(self.config_path),
        overrides=list(overrides),
        strict=True)
      del cfg['hydra']
      returns.append(cfg)
    return returns[0] if len(returns) == 1 else returns

  def hash(self, overrides=[], **configs) -> str:
    r""" An unique hash string of length 8 based on the parsed configurations.
    """
    overrides = _overrides(overrides) + _overrides(configs)
    cfg = self.load_configuration(overrides)
    if isinstance(cfg, DictConfig):
      return Experimenter.hash_config(cfg, self.exclude_keys)
    return [Experimenter.hash_config(cfg, self.exclude_keys) for c in cfg]

  def clear_all_experiments(self, verbose=True):
    input("<Enter> to continue remove all experiments ...")
    for path in os.listdir(self._save_path):
      path = os.path.join(self._save_path, path)
      if os.path.isdir(path):
        shutil.rmtree(path)
      elif os.path.isfile(path):
        os.remove(path)
      if verbose:
        print(" Remove:", path)
    return self

  ####################### Base methods
  def on_load_data(self, cfg: DictConfig):
    r""" Cleaning """
    pass

  def on_create_model(self, cfg: DictConfig):
    r""" Cleaning """
    pass

  def on_load_model(self, path: str):
    r""" Cleaning """
    pass

  def on_train(self, cfg: DictConfig):
    r""" Cleaning """
    pass

  def on_train(self, cfg: DictConfig, model_path: str):
    r""" Cleaning """
    pass

  ####################### Basic logics
  def _run(self, cfg: DictConfig):
    # the cfg is dispatched by hydra.run_job, we couldn't change anything here
    logger = LOGGER
    with warnings.catch_warnings():
      warnings.filterwarnings('ignore', category=DeprecationWarning)
      # prepare the paths
      model_path = self.get_model_path(cfg)
      md5_path = model_path + '.md5'
      # check the configs
      config_path = self.get_config_path(cfg, datetime=True)
      with open(config_path, 'w') as f:
        OmegaConf.save(cfg, f)
      logger.info("Save config: %s" % config_path)
      # load data
      self.on_load_data(cfg)
      logger.info("Loaded data")
      # create or load model
      if os.path.exists(model_path) and len(os.listdir(model_path)) > 0:
        # check if the loading model is consistent with the saved model
        if os.path.exists(md5_path) and self.consistent_model:
          md5_loaded = md5_folder(model_path)
          with open(md5_path, 'r') as f:
            md5_saved = f.read().strip()
          assert md5_loaded == md5_saved, \
            "MD5 of saved model mismatch, probably files are corrupted"
        model = self.on_load_model(model_path)
        if model is None:
          raise RuntimeError(
              "The implementation of on_load_model must return the loaded model."
          )
        logger.info("Loaded model: %s" % model_path)
      else:
        self.on_create_model(cfg)
        logger.info("Create model: %s" % model_path)
      # training
      self.on_train(cfg, model_path)
      logger.info("Finish training")
      # saving the model hash
      if os.path.exists(model_path) and len(os.listdir(model_path)) > 0:
        with open(md5_path, 'w') as f:
          f.write(md5_folder(model_path))
        logger.info("Save model:%s" % model_path)

  def run(self, overrides=[], ncpu=None, **configs):
    r"""

    Arguments:
      strict: A Boolean, strict configurations prevent the access to
        unknown key, otherwise, the config will return `None`.
    """
    overrides = _overrides(overrides) + _overrides(configs)
    strict = False
    if ncpu is None:
      ncpu = self.ncpu
    ncpu = int(ncpu)
    is_multirun = any(',' in ovr for ovr in overrides)

    def _run(self, config_file, task_function, overrides):
      if is_multirun:
        raise RuntimeError(
            "Performing single run with multiple overrides in hydra "
            "(use '-m' for multirun): %s" % str(overrides))
      cfg = self.compose_config(config_file=config_file,
                                overrides=overrides,
                                strict=strict,
                                with_log_configuration=True)
      HydraConfig().set_config(cfg)
      return run_job(
          config=cfg,
          task_function=task_function,
          job_dir_key="hydra.run.dir",
          job_subdir_key=None,
      )

    def _multirun(self, config_file, task_function, overrides):
      # Initial config is loaded without strict (individual job configs may have strict).
      from hydra._internal.plugins import Plugins
      cfg = self.compose_config(config_file=config_file,
                                overrides=overrides,
                                strict=strict,
                                with_log_configuration=True)
      HydraConfig().set_config(cfg)
      sweeper = Plugins.instantiate_sweeper(config=cfg,
                                            config_loader=self.config_loader,
                                            task_function=task_function)
      # override launcher for using multiprocessing
      sweeper.launcher = ParallelLauncher(ncpu=ncpu)
      sweeper.launcher.setup(config=cfg,
                             config_loader=self.config_loader,
                             task_function=task_function)
      return sweeper.sweep(arguments=cfg.hydra.overrides.task)

    old_multirun = (Hydra.run, Hydra.multirun)
    Hydra.run = _run
    Hydra.multirun = _multirun

    try:
      # append the new override
      if len(overrides) > 0:
        sys.argv += overrides
      # append the hydra log path
      job_fmt = "/${now:%d%b%y_%H%M%S}"
      sys.argv.append("hydra.run.dir=%s" % self.get_hydra_path() + job_fmt)
      sys.argv.append("hydra.sweep.dir=%s" % self.get_hydra_path() + job_fmt)
      sys.argv.append("hydra.sweep.subdir=${hydra.job.id}")
      # sys.argv.append(r"hydra.job_logging.formatters.simple.format=" +
      #                 r"[\%(asctime)s][\%(name)s][\%(levelname)s] - \%(message)s")
      args_parser = get_args_parser()
      run_hydra(
          args_parser=args_parser,
          task_function=self._run,
          config_path=self.config_path,
          strict=strict,
      )
    except KeyboardInterrupt:
      sys.exit(-1)
    except SystemExit:
      pass
    Hydra.run = old_multirun[0]
    Hydra.multirun = old_multirun[1]
    return self

  ####################### For evaluation
  def search(self, conditions={}):
    path = self._save_path
    conditions = [cond.split('=') for cond in _overrides(conditions)]
    conditions = {
        key: set([i.strip() for i in values.split(',')
                 ]) for key, values in conditions
    }
    exp_path = [
        os.path.join(path, name)
        for name in os.listdir(path)
        if 'exp_' == name[:4]
    ]
    exp_path = list(
        filter(lambda x: os.path.isdir(os.path.join(x, 'model')), exp_path))
    found_models = []
    for path in exp_path:
      cfg = sorted([
          os.path.join(path, i) for i in os.listdir(path) if 'configs_' == i[:8]
      ],
                   key=lambda x: get_formatted_datetime(
                       only_number=False,
                       convert_text=x.split('_')[-1].split('.')[0]).timestamp())
      cfg = cfg[-1]  # lastest config
      with open(cfg, 'r') as f:
        cfg = OmegaConf.load(f)
      # filter the conditions
      if all(cfg[key] in val for key, val in conditions.items()):
        found_models.append(os.path.join(path, 'model'))
    # ====== load the found models ====== #
    if len(found_models) == 0:
      raise RuntimeError("Cannot find model satisfying conditions: %s" %
                         str(conditions))
    models = [self.on_load_model(path) for path in found_models]
    return models[0] if len(models) == 1 else models
