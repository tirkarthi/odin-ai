import dataclasses
import os
import shutil
from functools import partial
from typing import Optional, Union, Sequence

import numpy as np
import tensorflow as tf
from matplotlib import pyplot as plt
from tensorflow.python.keras import Sequential, Input
from tensorflow.python.keras.layers import Dense, Flatten, Concatenate, Conv2D, \
  Conv2DTranspose, GlobalAvgPool2D, BatchNormalization, Activation, Reshape
from tensorflow_probability.python.distributions import Normal, Independent
from tensorflow_probability.python.layers import DistributionLambda

from odin import visual as vs
from odin.bay import DistributionDense, MVNDiagLatents, kl_divergence, \
  DisentanglementGym, VariationalAutoencoder, get_vae
from odin.bay.layers import RelaxedOneHotCategoricalLayer
from odin.bay.vi import traverse_dims
from odin.fuel import get_dataset, ImageDataset
from odin.networks import get_networks, SequentialNetwork, TrainStep
from odin.utils import as_tuple
from utils import get_args, train, run_multi, set_cfg, Arguments, \
  get_model_path, get_results_path, Callback, prepare_images

# ===========================================================================
# Const and helper
# ===========================================================================
config: Optional[Arguments] = None


def networks(input_dim: Union[None, int], name: str,
             batchnorm: bool = False) -> Sequential:
  if batchnorm:
    layers = [
      Dense(512, use_bias=False, name=f'{name}_1'),
      BatchNormalization(),
      Activation('relu'),
      Dense(512, use_bias=False, name=f'{name}_2'),
      BatchNormalization(),
      Activation('relu'),
    ]
  else:
    layers = [
      Dense(512, activation='relu', name=f'{name}_1'),
      Dense(512, activation='relu', name=f'{name}_2'),
    ]
  if input_dim is not None:
    layers = [Input([input_dim])] + layers
  return Sequential(layers, name=name)


def to_elbo(semafo, llk, kl):
  elbo = semafo.elbo(llk, kl)
  return tf.reduce_mean(-elbo), \
         {k: tf.reduce_mean(v) for k, v in dict(**llk, **kl).items()}


# ===========================================================================
# SemafoVAE
# ===========================================================================
@dataclasses.dataclass
class LatentConfig:
  encoder: int = 1
  decoder: int = 1
  filters: int = 32  # also the number of latent units
  kernel: int = 4  # recommend kernel is multiply of stride
  strides: int = 2
  post: Optional[Conv2D] = None
  prior: Optional[Conv2D] = None
  deter: Optional[Conv2D] = None
  out: Optional[Conv2DTranspose] = None
  pz: Optional[DistributionLambda] = None
  qz: Optional[DistributionLambda] = None
  beta: float = 1.0
  C: Optional[float] = None

  def initialize(self, decoder: Conv2D):
    idx = self.decoder
    cnn_cfg = dict(filters=2 * self.filters,  # for mean and stddev
                   strides=self.strides,
                   kernel_size=self.kernel,
                   padding='same')
    if self.post is None:
      self.post = Conv2D(**cnn_cfg, name=f'Posterior{idx}')
    if self.prior is None:
      self.prior = Conv2D(**cnn_cfg, name=f'Prior{idx}')
    if self.deter is None:
      self.deter = Conv2D(**cnn_cfg, name=f'Deterministic{idx}')
    if self.out is None:
      self.out = Conv2DTranspose(filters=decoder.filters,
                                 kernel_size=self.kernel,
                                 strides=self.strides,
                                 padding='same',
                                 name=f'Output{idx}')
    if self.pz is None:
      self.pz = DistributionLambda(_create_normal, name=f'pz{idx}')
    if self.qz is None:
      self.qz = DistributionLambda(_create_normal, name=f'qz{idx}')
    return self


def _create_normal(params):
  loc, scale = tf.split(params, 2, axis=-1)
  scale = tf.nn.softplus(scale) + tf.cast(tf.exp(-7.), params.dtype)
  d = Normal(loc, scale)
  d = Independent(d, reinterpreted_batch_ndims=loc.shape.rank - 1)
  return d


DefaultHierarchy = dict(
  shapes3d=dict(),
  dsprites=dict(),
  cifar10=dict(),
  fashionmnist=dict(encoder=3, decoder=3,
                    filters=16, kernel=14, strides=7),
  mnist=dict(encoder=3, decoder=3,
             filters=16, kernel=14, strides=7),
)

DefaultGamma = dict(
  fashionmnist=5.,
  mnist=5.,
)

DefaultGammaPy = dict(
  fashionmnist=10.,
  mnist=10,
)


class SemafoVAE(VariationalAutoencoder):

  def __init__(self,
               encoder: SequentialNetwork,
               decoder: SequentialNetwork,
               labels: DistributionDense,
               coef_H_qy: float = 1.,
               gamma_py: float = None,
               gamma_uns: Optional[float] = None,
               gamma_sup: float = 1.,
               beta_uns: float = 1.,
               beta_sup: float = 1.,
               coef_deter: float = 0.,
               n_iw_y: int = 1,
               **kwargs):
    super().__init__(encoder=encoder, decoder=decoder, **kwargs)
    self.encoder.track_outputs = True
    if not self.decoder.built:
      self.decoder(Input(self.latents.event_shape))
    self.decoder.track_outputs = True
    # === 0. other parameters
    if gamma_py is None:
      gamma_py = DefaultGammaPy[config.ds]
    if gamma_uns is None:
      gamma_uns = DefaultGamma[config.ds]
    self.n_iw_y = int(n_iw_y)
    self.coef_H_qy = float(coef_H_qy)
    self.coef_deter = float(coef_deter)
    self.gamma_uns = float(gamma_uns)
    self.gamma_sup = float(gamma_sup)
    self.gamma_py = float(gamma_py)
    self.beta_uns = float(beta_uns)
    self.beta_sup = float(beta_sup)
    self.labels_org = labels
    # === 1. fixed utility layers
    self.flatten = Flatten()
    self.concat = Concatenate(-1)
    self.global_avg = GlobalAvgPool2D()
    # === 2. reparameterized q(y|z)
    ydim = int(np.prod(self.labels_org.event_shape))
    zdim = int(np.prod(self.latents.event_shape))

    self.labels = SequentialNetwork([
      # networks(None, 'EncoderY', batchnorm=True),
      DistributionDense(event_shape=[ydim], projection=True,
                        posterior=RelaxedOneHotCategoricalLayer,
                        posterior_kwargs=dict(temperature=0.5),
                        name='Digits')],
      name='qy_z')
    # === 3. second VAE for y and u
    self.encoder2 = networks(ydim, 'Encoder2')
    self.latents2 = MVNDiagLatents(units=ydim, name='Latents2')
    self.latents2(self.encoder2.output)
    self.decoder2 = networks(self.latents2.event_size, 'Decoder2')
    self.observation2 = DistributionDense(**self.labels_org.get_config())
    self.observation2(self.decoder2.output)
    # === 4. p(z|u)
    self.latents_prior = SequentialNetwork([
      networks(ydim, 'Prior'),
      MVNDiagLatents(zdim)
    ], name=f'{self.latents.name}_prior')
    self.latents_prior.build([None] + self.latents2.event_shape)

  def encode(self, inputs, training=None, **kwargs):
    latents = super().encode(inputs, training=training, **kwargs)
    latents.last_outputs = [i for _, i in self.encoder.last_outputs]
    return latents

  def decode(self, latents, training=None, **kwargs):
    z = tf.convert_to_tensor(latents)
    px_z = super().decode(z, training=training, **kwargs)
    qy_z = self.labels(z, training=training)
    return px_z, qy_z

  def encode_aux(self, y, training=None):
    h_e = self.encoder2(y, training=training)
    qu = self.latents2(h_e, training=training)
    return qu

  def decode_aux(self, qu, training=None):
    h_d = self.decoder2(qu, training=training)
    py = self.observation2(h_d, training=training)
    return py

  def call_aux(self, y, training=None):
    qu = self.encode_aux(y, training=training)
    py = self.decode_aux(qu, training=training)
    return py, qu

  def sample_prior(self, n=1, seed=1, return_distribution=False, **kwargs):
    y = np.diag([1.] * 10)
    y = np.tile(y, [int(np.ceil(n / 10)), 1])[:n]
    qu_y = self.encode_aux(y, training=False)
    u = qu_y.sample(seed=seed)
    pz_u = self.latents_prior(u, training=False)
    if return_distribution:
      return pz_u
    return pz_u.mean()

  @classmethod
  def is_semi_supervised(cls) -> bool:
    return True

  def get_latents(self, inputs=None, training=None, mask=None,
                  return_prior=False, **kwargs):
    if inputs is None:
      if self.last_outputs is None:
        raise RuntimeError(f'Cannot get_latents of {self.name} '
                           'both inputs and last_outputs are None.')
      (px, qy), qz = self.last_outputs
    else:
      (px, qy), qz = self(inputs, training=training, mask=mask, **kwargs)
    posterior = qz
    prior = self.latents_prior(self.encode_aux(qy, training=training),
                               training=training)
    if return_prior:
      return posterior, prior
    return posterior

  def elbo_components(self, inputs, training=None, mask=None, **kwargs):
    llk, kl = super().elbo_components(inputs, training=training, mask=mask,
                                      **kwargs)
    px, qz = self.last_outputs
    # for hierarchical VAE
    if hasattr(px, 'kl_pairs'):
      for cfg, q, p in px.kl_pairs:
        kl[f'kl_z{cfg.decoder}'] = cfg.beta * kl_divergence(
          q, p, analytic=self.analytic)
    return llk, kl

  def train_steps(self, inputs, training=None, mask=None, name='', **kwargs):
    x_u, x_s, y_s = inputs

    # === 1. Supervised
    def elbo_sup():
      (px_z, qy_z), qz_x = self(x_s, training=training)
      py_u, qu_y = self.call_aux(y_s, training=training)
      pz_u = self.latents_prior(qu_y, training=training)

      llk = dict(
        llk_px_sup=self.gamma_sup * px_z.log_prob(x_s),
        llk_py_sup=self.gamma_sup * self.gamma_py *
                   py_u.log_prob(y_s),
        llk_qy_sup=self.gamma_sup * self.gamma_py *
                   qy_z.log_prob(tf.clip_by_value(y_s, 1e-6, 1. - 1e-6))
      )

      z = tf.convert_to_tensor(qz_x)
      kl = dict(
        kl_z_sup=self.beta_sup * (qz_x.log_prob(z) - pz_u.log_prob(z)),
        kl_u_sup=self.beta_sup * qu_y.KL_divergence(analytic=self.analytic)
      )
      # for hierarchical VAE
      if hasattr(px_z, 'kl_pairs'):
        for i, (cfg, q, p) in enumerate(px_z.kl_pairs):
          kl_qp = kl_divergence(q, p, analytic=self.analytic)
          if cfg.C is not None:
            kl_qp = tf.math.abs(kl_qp - cfg.C * np.prod(q.event_shape))
          kl[f'kl_z{i + 1}'] = cfg.beta * kl_qp

      return to_elbo(self, llk, kl)

    yield TrainStep(parameters=self.trainable_variables, func=elbo_sup)

    # === 2. Unsupervised
    def elbo_uns():
      (px_z, qy_z), qz_x = self(x_u, training=training)
      y_u = tf.convert_to_tensor(qy_z, dtype=self.dtype)
      py_u, qu_y = self.call_aux(y_u, training=training)
      pz_u = self.latents_prior(qu_y, training=training)

      llk = dict(
        llk_px_uns=self.gamma_uns * px_z.log_prob(x_u),
        llk_py_uns=self.gamma_uns * self.gamma_py *
                   py_u.log_prob(y_u)
      )

      z = tf.convert_to_tensor(qz_x)
      kl = dict(
        kl_z_uns=self.beta_uns * (qz_x.log_prob(z) - pz_u.log_prob(z)),
        kl_u_uns=self.beta_uns * qu_y.KL_divergence(analytic=self.analytic),
        llk_qy_uns=self.coef_H_qy *
                   qy_z.log_prob(tf.clip_by_value(y_u, 1e-6, 1. - 1e-6))
      )
      # for hierarchical VAE
      if hasattr(px_z, 'kl_pairs'):
        for i, (cfg, q, p) in enumerate(px_z.kl_pairs):
          kl_qp = kl_divergence(q, p, analytic=self.analytic)
          if cfg.C is not None:
            kl_qp = tf.math.abs(kl_qp - cfg.C * np.prod(q.event_shape))
          kl[f'kl_z{i + 1}'] = cfg.beta * kl_qp

      return to_elbo(self, llk, kl)

    yield TrainStep(parameters=self.trainable_variables, func=elbo_uns)


# ===========================================================================
# Hierarchical
# ===========================================================================
class SemafoHVAE(SemafoVAE):

  def __init__(self,
               hierarchy: Optional[Sequence[LatentConfig]] = None,
               **kwargs):
    super().__init__(**kwargs)
    self._is_sampling = False
    if hierarchy is None:
      hierarchy = LatentConfig(**DefaultHierarchy[config.ds])
    self.hierarchy = {
      cfg.decoder: cfg.initialize(self.decoder.layers[cfg.decoder])
      for cfg in as_tuple(hierarchy)}

  @classmethod
  def is_hierarchical(cls) -> bool:
    return True

  def set_sampling(self, is_sampling: bool):
    self._is_sampling = bool(is_sampling)
    return self

  def decode(self, latents, training=None, **kwargs):
    if isinstance(latents, (tuple, list)):
      priors = list(latents[1:])
      priors = priors + [None] * (len(self.hierarchy) - len(priors))
      latents = latents[0]
    else:
      priors = [None] * len(self.hierarchy)
    # === 0. prepare
    z_org = tf.convert_to_tensor(latents)
    z = z_org
    kl_pairs = []
    z_prev = [z_org]
    # === 0. hierarchical latents
    for idx, layer in enumerate(self.decoder.layers):
      z = layer(z, training=training)
      if idx in self.hierarchy:
        z_layers: LatentConfig = self.hierarchy[idx]
        z_samples = priors.pop(0)
        if z_samples is None:
          # prior
          h_prior = z_layers.prior(z, training=training)
          pz = z_layers.pz(h_prior, training=training)
          # posterior (inference mode)
          if not self._is_sampling:
            if not hasattr(latents, 'last_outputs'):
              raise RuntimeError(
                'No encoder states found for hierarchical model')
            h_e = latents.last_outputs[z_layers.encoder]
            h_post = z_layers.post(self.concat([z, h_e]), training=training)
            qz = z_layers.qz(h_post, training=training)
          # sampling mode (just use the prior)
          else:
            qz = pz
          # track the post and prior for KL
          kl_pairs.append((z_layers, qz, pz))
          z_samples = tf.convert_to_tensor(qz)
        # output
        z_prev.append(z_samples)
        if self.coef_deter > 0:
          h_deter = z_layers.deter(z, training=training)
          z_samples = self.concat([z_samples, self.coef_deter * h_deter])
        z = z_layers.out(z_samples, training=training)
    # === 1. p(x|z0,z1)
    px_z = self.observation(z, training=training)
    px_z.kl_pairs = kl_pairs
    # === 2. q(y|z)
    py_z = self.labels(z_org, training=training)
    return px_z, py_z

  def get_latents(self, inputs=None, training=None, mask=None,
                  return_prior=False, **kwargs):
    is_sampling = self._is_sampling
    self.set_sampling(False)
    if inputs is None:
      if self.last_outputs is None:
        raise RuntimeError(f'Cannot get_latents of {self.name} '
                           'both inputs and last_outputs are None.')
      (px, qy), qz = self.last_outputs
    else:
      (px, qy), qz = self(inputs, training=training, mask=mask, **kwargs)
    self.set_sampling(is_sampling)
    posterior = [qz]
    prior = [self.latents_prior(self.encode_aux(qy, training=training),
                                training=training)]
    for cfg, q, p in px.kl_pairs:
      posterior.append(q)
      prior.append(p)
    if return_prior:
      return posterior, prior
    return posterior

  def sample_traverse(self, inputs, n_traverse_points=11, n_best_latents=5,
                      min_val=-2.0, max_val=2.0, mode='linear',
                      smallest_stddev=True, training=False, mask=None):
    from odin.bay.vi import traverse_dims
    latents = self.encode(inputs, training=training, mask=mask)
    stddev = np.sum(latents.stddev(), axis=0)
    # smaller stddev is better
    if smallest_stddev:
      top_latents = np.argsort(stddev)[:int(n_best_latents)]
    else:
      top_latents = np.argsort(stddev)[::-1][:int(n_best_latents)]
    # keep the encoder states for the posteriors
    last_outputs = [i for i in latents.last_outputs]
    latents = traverse_dims(latents,
                            feature_indices=top_latents,
                            min_val=min_val, max_val=max_val,
                            n_traverse_points=n_traverse_points,
                            mode=mode)
    latents = tf.convert_to_tensor(latents)
    if not self._is_sampling:
      n_tiles = n_traverse_points * len(top_latents)
      last_outputs = [
        tf.tile(i, [n_tiles] + [1 for _ in range(i.shape.rank - 1)])
        for i in last_outputs]
      latents.last_outputs = last_outputs
    return self.decode(latents, training=training, mask=mask), top_latents


class SemafoHVAEG10(SemafoHVAE):

  def __init__(self, **kwargs):
    super().__init__(gamma_uns=10., coef_deter=0.0, **kwargs)


class SemafoHVAEG2(SemafoHVAE):

  def __init__(self, **kwargs):
    super().__init__(gamma_uns=2., coef_deter=0.0, **kwargs)


class SemafoHVAEG3(SemafoHVAE):

  def __init__(self, **kwargs):
    super().__init__(gamma_uns=3., coef_deter=0.0, **kwargs)


class SemafoHVAEdeter(SemafoHVAE):

  def __init__(self, **kwargs):
    super().__init__(coef_deter=0.5, **kwargs)


class SemafoHVAEdeter1(SemafoHVAE):

  def __init__(self, **kwargs):
    super().__init__(coef_deter=1.0, **kwargs)


# ===========================================================================
# Training
# ===========================================================================
def _mean(dists, idx):
  m = dists[idx].mean()
  return tf.reshape(m, (m.shape[0], -1))


def main(args: Arguments):
  ds = get_dataset(args.ds)
  model = None
  for k, v in globals().items():
    if k.lower() == args.vae and callable(v):
      model = v
      break
  if model is None:
    model = get_vae(model)
  is_semi = model.is_semi_supervised()
  # === 0. build the model
  model: VariationalAutoencoder = model(
    **get_networks(args.ds, is_semi_supervised=is_semi))
  model.build((None,) + ds.shape)
  # === 1. training
  if not args.eval:
    train(model, ds, args,
          label_percent=args.py if is_semi else 0.0,
          on_batch_end=(),
          on_valid_end=(Callback.save_best_llk,),
          oversample_ratio=args.ratio)
  # === 2. evaluation
  else:
    path = get_results_path(args)
    if args.override:
      for p in [path, path + '_valid']:
        if os.path.exists(p):
          print('Override results at path:', p)
          shutil.rmtree(p)
    if not os.path.exists(path):
      os.makedirs(path)
    ### load model weights
    model.load_weights(get_model_path(args), raise_notfound=True, verbose=True)
    gym = DisentanglementGym(model=model,
                             dataset=args.ds,
                             batch_size=args.bs,
                             dpi=args.dpi,
                             seed=args.seed)

    ### special case for SemafoVAE
    if isinstance(model, SemafoVAE):
      pz = model.sample_prior(n=10, return_distribution=True)
      mean = pz.mean()
      stddev = pz.stddev()
      n_points = 31
      n_latents = 12
      max_std = 3.
      if model.is_hierarchical():
        model.set_sampling(True)
      for i in range(10):
        m = mean[i:i + 1]
        s = stddev[i].numpy()
        ids = np.argsort(s)[::-1][:n_latents]  # higher is better
        m = traverse_dims(m,
                          feature_indices=ids,
                          min_val=-max_std * s,
                          max_val=max_std * s,
                          n_traverse_points=n_points)
        img, _ = model.decode(m)
        # plotting
        plt.figure(figsize=(1.5 * 11, 1.5 * n_latents))
        vs.plot_images(prepare_images(img.mean(), normalize=True),
                       grids=(n_latents, n_points),
                       ax=plt.gca())
      vs.plot_save(os.path.join(path, 'prior_traverse.pdf'), verbose=True)
      # special case for hierarchical model
      if model.is_hierarchical():
        model.set_sampling(True)
        for i in range(10):
          z0 = mean[i:i + 1]
          img, _ = model.decode(z0)
          # traverse the second latents prior
          pz1 = img.kl_pairs[0][2]
          shape = list(pz1.event_shape)
          s1 = np.ravel(pz1.stddev().numpy()[0])
          ids = np.argsort(s1)[::-1][:n_latents]  # higher is better
          z1 = np.reshape(pz1.mean(), (1, -1))
          z1 = traverse_dims(z1,
                             feature_indices=ids,
                             min_val=-max_std * s1,
                             max_val=max_std * s1,
                             n_traverse_points=n_points)
          z0 = tf.tile(z0, [z1.shape[0], 1])
          z1 = np.reshape(z1, [-1] + shape)
          img, _ = model.decode([z0, z1])
          # plotting
          plt.figure(figsize=(1.5 * 11, 1.5 * n_latents))
          vs.plot_images(prepare_images(img.mean(), normalize=True),
                         grids=(n_latents, n_points),
                         ax=plt.gca())
        vs.plot_save(os.path.join(path, 'prior1_traverse.pdf'), verbose=True)

    # important, otherwise, all evaluation is wrong
    if isinstance(model, SemafoHVAE):
      model.set_sampling(False)

    ### run the prediction for test set
    with gym.run_model(n_samples=1800 if args.debug else -1,
                       partition='test'):
      print('Accuracy:', gym.accuracy_score())
      print('LLK     :', gym.log_likelihood())
      print('KL      :', gym.kl_divergence())
      # latents t-SNE
      gym.plot_latents_tsne()
      if gym.n_latent_vars > 1:
        for i in range(gym.n_latent_vars):
          gym.plot_latents_tsne(convert_fn=partial(_mean, idx=i),
                                title=f'_z{i}')
      # prior sampling
      if model.is_hierarchical():
        model.set_sampling(True)
      gym.plot_latents_sampling()
      # traverse
      if model.is_hierarchical():
        model.set_sampling(True)
        gym.plot_latents_traverse(title='_prior')
        model.set_sampling(False)
        gym.plot_latents_traverse(title='_post')
      else:
        gym.plot_latents_traverse()
      # inference
      for i in range(gym.n_latent_vars):
        gym.plot_latents_stats(latent_idx=i)
        gym.plot_latents_factors(
          convert_fn=partial(_mean, idx=i), title=f'_z{i}')
        gym.plot_correlation(
          convert_fn=partial(_mean, idx=i), title=f'_z{i}')
      gym.plot_reconstruction()
    gym.save_figures(path, verbose=True)


# ===========================================================================
# Main
# ===========================================================================
if __name__ == '__main__':
  set_cfg(root_path='/home/trung/exp/semafo')
  args = get_args(dict(py=0.004, ratio=0.1, it=400000))
  config = args
  run_multi(main, args=args)
