from typing import Union
from warnings import warn

import numpy as np
import tensorflow as tf
from odin.backend.maths import softplus1
from odin.bay.layers.dense_distribution import DistributionDense
from odin.bay.random_variable import RVmeta
from odin.bay.vi.autoencoder.beta_vae import betaVAE
from tensorflow.python.keras.layers import Layer
from tensorflow_probability.python.distributions import (PowerSpherical,
                                                         SphericalUniform,
                                                         VonMisesFisher)
from tensorflow_probability.python.layers import DistributionLambda


class _von_mises_fisher:

  def __init__(self, event_size):
    self.event_size = int(event_size)

  def __call__(self, x):
    # use softplus1 for concentration to prevent collapse and instability with
    # small concentration
    # note in the paper:
    # z_var = tf.layers.dense(h1, units=1, activation=tf.nn.softplus) + 1
    return VonMisesFisher(
        mean_direction=tf.math.l2_normalize(x[..., :self.event_size], axis=-1),
        concentration=softplus1(x[..., -1]),
    )


class _power_spherical:

  def __init__(self, event_size):
    self.event_size = int(event_size)

  def __call__(self, x):
    return PowerSpherical(
        mean_direction=tf.math.l2_normalize(x[..., :self.event_size], axis=-1),
        concentration=softplus1(x[..., -1]),
    )


class hypersphericalVAE(betaVAE):

  def __init__(self,
               latents: Union[RVmeta, Layer] = RVmeta(64, name="latents"),
               distribution: str = 'powerspherical',
               name: str = 'HyperSphericalVAE',
               **kwargs):
    event_shape = latents.event_shape
    event_size = int(np.prod(event_shape))
    distribution = str(distribution).lower()
    assert distribution in ('powerspherical', 'vonmisesfisher'), \
      ('Support PowerSpherical or VonMisesFisher distribution, '
       f'but given: {distribution}')
    if distribution == 'powerspherical':
      fn_distribution = _power_spherical(event_size)
    else:
      fn_distribution = _von_mises_fisher(event_size)
      if event_size != 3:
        raise ValueError('VonMisesFisher distribution only reparamerizable at '
                         f'latent_size=3, but given {event_size}')
    latents = DistributionDense(
        event_shape,
        posterior=DistributionLambda(make_distribution_fn=fn_distribution),
        prior=SphericalUniform(dimension=event_size),
        units=event_size + 1,
        name=latents.name)
    super().__init__(latents=latents, analytic=True, name=name, **kwargs)


class powersphericalVAE(hypersphericalVAE):

  def __init__(self, name: str = 'PowerSphericalVAE', **kwargs):
    super().__init__(distribution='powerspherical', name=name, **kwargs)
