from __future__ import division, absolute_import, print_function

import os
import math
import numbers
import cPickle
from collections import OrderedDict

import numpy as np

import tensorflow as tf

from odin.config import CONFIG, RNG_GENERATOR
from odin.utils import as_tuple, as_shape_tuple, dict_union
from odin.basic import (add_role, TRAINING, PARAMETER,
                        ACTIVATION_PARAMETER, DEPLOYING,
                        add_shape, get_shape)

from .helpers import (get_session, as_tensor_variable, ComputationGraph)
FLOATX = CONFIG.floatX
EPSILON = CONFIG.epsilon
NPROCESSORS = CONFIG['device_info']['n']
_RNG = np.random.RandomState(seed=RNG_GENERATOR.randint(10e8))


def _normalize_axis(axis, ndim):
    if axis is None:
        return None
    if isinstance(axis, (tuple, list)):
        return tuple([a % ndim if a is not None else a
                for a in axis])
    return axis % ndim


def eval(x):
    '''Evaluates the value of a tensor.
    Returns a Numpy array.
    '''
    return x.eval(session=get_session())


# ===========================================================================
# Basic ops
# ===========================================================================
def backend_ops_relu(x, alpha=0.):
    # Adapted implementation from theano
    if alpha == 0:
        return tf.nn.relu(x)
    else:
        # We can't use 0.5 and 1 for one and half.  as if alpha is a
        # numpy dtype, they will be considered as float64, so would
        # cause upcast to float64.
        alpha = as_tensor_variable(alpha, dtype=x.dtype.base_dtype)
        f1 = 0.5 * (1 + alpha)
        f2 = 0.5 * (1 - alpha)
        return f1 * x + f2 * tf.abs(x)


def backend_ops_elu(x, alpha):
    res = tf.nn.elu(x)
    if alpha != 1:
        res = tf.select(x > 0, res, alpha * res)
    return res


def backend_ops_hard_sigmoid(x):
    slope = tf.constant(0.2, dtype=x.dtype.base_dtype)
    shift = tf.constant(0.5, dtype=x.dtype.base_dtype)
    x = (x * slope) + shift
    x = tf.clip_by_value(x, 0., 1.)
    return x

backend_ops_softmax = tf.nn.softmax
backend_ops_softplus = tf.nn.softplus
backend_ops_softsign = tf.nn.softsign
backend_ops_sigmoid = tf.nn.sigmoid
backend_ops_tanh = tf.nn.tanh

backend_ops_square = tf.square
backend_ops_abs = tf.abs
backend_ops_sign = tf.sign
backend_ops_inv = tf.inv
backend_ops_sqrt = tf.sqrt
backend_ops_exp = tf.exp
backend_ops_log = tf.log
backend_ops_round = tf.round
backend_ops_pow = tf.pow
backend_ops_clip = tf.clip_by_value

backend_ops_diag = tf.diag_part

backend_ops_categorical_crossentropy = tf.nn.softmax_cross_entropy_with_logits
backend_ops_binary_crossentropy = tf.nn.sigmoid_cross_entropy_with_logits


def backend_ops_eye(n, m, dtype):
    x = tf.Variable(initial_value=np.eye(n, m, dtype=dtype), dtype=dtype)
    get_session().run(x.initializer)
    return x

# Comparator
backend_ops_switch = tf.select
backend_ops_eq = tf.equal
backend_ops_neq = tf.not_equal
backend_ops_gt = tf.greater
backend_ops_ge = tf.greater_equal
backend_ops_lt = tf.less
backend_ops_le = tf.less_equal


# ===========================================================================
# Shape operator
# ===========================================================================
def broadcastable(x):
    return x


def addbroadcast(x, *axes):
    return x


# ===========================================================================
# Predefined data
# ===========================================================================
def zeros(shape, dtype=FLOATX, name=None):
    """Instantiate an all-zeros variable.
    """
    x = tf.zeros(shape, dtype=dtype, name=name)
    return x


def ones(shape, dtype=FLOATX, name=None):
    """Instantiate an all-ones variable.
    """
    x = tf.ones(shape, dtype=dtype, name=name)
    return x


def ones_like(x, dtype=None):
    if dtype is None:
        dtype = x.dtype.base_dtype
    x = tf.ones_like(x, dtype=dtype, optimize=True)
    return x


def zeros_like(x, dtype=None):
    if dtype is None:
        dtype = x.dtype.base_dtype
    x = tf.zeros_like(x, dtype=dtype, optimize=True)
    return x


def cast(x, dtype):
    if 'tensorflow.' in str(x.__class__):
        return tf.cast(x, dtype)
    return np.cast[dtype](x)


# ===========================================================================
# LINEAR ALGEBRA
# Assumed overridden:
# +, -, /, *, +=, -=, *=, /=
# ===========================================================================
def dot(x, y):
    '''Multiplies 2 tensors.
    When attempting to multiply a ND tensor
    with a ND tensor, reproduces the Theano behavior
    (e.g. (2, 3).(4, 3, 5) = (2, 4, 5))
    '''
    shapeX = get_shape(x)
    shapeY = get_shape(y)
    ndimX = x.get_shape().ndims
    ndimY = y.get_shape().ndims
    if ndimX > 2:
        x = tf.reshape(x, (-1, shapeX[-1]))
    if ndimY > 2:
        y_dims = list(range(ndimY))
        y_dims = [y_dims.pop(-2)] + y_dims
        y = tf.transpose(y, perm=y_dims)
        y = tf.reshape(y, (shapeY[-2], -1))
        outshapeY = tuple([shapeY[i] for i in y_dims[1:]])
    else:
        outshapeY = (shapeY[-1],)
    # calculate dot product and desire shape
    output_shape = shapeX[:-1] + outshapeY
    output = tf.reshape(tf.matmul(x, y), output_shape)
    return output


def batched_dot(x, y):
    """Batchwise dot product.
    This function computes the dot product between the two tensors,
    by iterating over the first dimension.
    """
    shapeX = get_shape(x)
    shapeY = get_shape(y)
    ndimX = x.get_shape().ndims
    ndimY = y.get_shape().ndims
    # same as dot but one more batch dimension
    if ndimX > 2 + 1:
        x = tf.reshape(x, (-1, np.prod(shapeX[1:-1]), shapeX[-1]))
    if ndimY > 2 + 1:
        y_dims = list(range(ndimY))
        y_dims = [y_dims.pop(0), y_dims.pop(-2)] + y_dims
        y = tf.transpose(y, perm=y_dims)
        outshapeY = tuple([shapeY[i] for i in y_dims[2:]])
        y = tf.reshape(y, (-1, shapeY[-2], np.prod(outshapeY)))
    else:
        outshapeY = (shapeY[-1],)
    # calculate dot product and desire shape
    output_shape = shapeX[:-1] + outshapeY
    output = tf.reshape(tf.batch_matmul(x, y, adj_x=None, adj_y=None),
                        [i if i is not None else -1 for i in output_shape])
    return output


def transpose(x, axes=None):
    """ Transposes a matrix. """
    return tf.transpose(x, perm=axes)


def gather(reference, indices):
    """Retrieves the vectors of indices `indices`
    in the 2D tensor `reference`.

    # Arguments
        reference: a 2D tensor.
        indices: an int tensor of indices.

    # Returns
        A 3D tensor of same type as `reference`.
    """
    return tf.gather(reference, indices)


# ===========================================================================
# ELEMENT-WISE OPERATIONS
# ===========================================================================
def var(x, axis=None, keepdims=False):
    axis = _normalize_axis(axis, x.get_shape().ndims)
    x = tf.cast(x, FLOATX)
    m = tf.reduce_mean(x, reduction_indices=axis, keep_dims=True)
    devs_squared = tf.square(x - m)
    return tf.reduce_mean(devs_squared,
                          reduction_indices=axis,
                          keep_dims=keepdims)


def mean(x, axis=None, keepdims=False):
    axis = _normalize_axis(axis, x.get_shape().ndims)
    x = tf.cast(x, FLOATX)
    return tf.reduce_mean(x, reduction_indices=axis, keep_dims=keepdims)


def std(x, axis=None, keepdims=False):
    return tf.sqrt(var(x, axis=axis, keepdims=keepdims))


def max(x, axis=None, keepdims=False):
    axis = _normalize_axis(axis, x.get_shape().ndims)
    return tf.reduce_max(x, reduction_indices=axis, keep_dims=keepdims)


def min(x, axis=None, keepdims=False):
    axis = _normalize_axis(axis, x.get_shape().ndims)
    return tf.reduce_min(x, reduction_indices=axis, keep_dims=keepdims)


def sum(x, axis=None, keepdims=False):
    """Sum of the values in a tensor, alongside the specified axis.
    """
    axis = _normalize_axis(axis, x.get_shape().ndims)
    return tf.reduce_sum(x, reduction_indices=axis, keep_dims=keepdims)


def prod(x, axis=None, keepdims=False):
    """Multiply the values in a tensor, alongside the specified axis.
    """
    axis = _normalize_axis(axis, x.get_shape().ndims)
    return tf.reduce_prod(x, reduction_indices=axis, keep_dims=keepdims)


def any(x, axis=None, keepdims=False):
    """Bitwise reduction (logical OR).
    """
    axis = _normalize_axis(axis, x.get_shape().ndims)
    return tf.reduce_any(x, reduction_indices=axis, keep_dims=keepdims)


def argmax(x, axis=-1, keepdims=False):
    axis %= x.get_shape().ndims
    x = tf.argmax(x, axis)
    if keepdims:
        x = tf.expand_dims(x, axis)
    return x


def argmin(x, axis=-1, keepdims=False):
    axis %= x.get_shape().ndims
    x = tf.argmin(x, axis)
    if keepdims:
        x = tf.expand_dims(x, axis)
    return x


def arange(start, stop=None, step=1, dtype=None):
    x = tf.range(start, limit=stop, delta=step)
    if dtype is not None:
        x = tf.cast(x, dtype)
    return x


def argsort(x, axis=-1):
    raise NotImplementedError


def argtop_k(x, k=1):
    # top-k accuracy
    return tf.nn.top_k(x, k=k, sorted=True)


# ===========================================================================
# Primitive ops
# ===========================================================================
def add(x, y):
    return tf.add(x, y)


def sub(x, y):
    return tf.sub(x, y)


def mul(x, y):
    return tf.mul(x, y)


def div(x, y):
    return tf.div(x, y)


def mod(x, y):
    return tf.mod(x, y)


def maximum(x, y):
    return tf.maximum(x, y)


def minimum(x, y):
    return tf.minimum(x, y)


# ===========================================================================
# SHAPE OPERATIONS
# ===========================================================================
def reverse(x, axes=-1):
    """Apply [::-1] to appropriate axis"""
    if not isinstance(axes, (tuple, list)):
        axes = (axes,)
    ndim = x.get_shape().ndims
    axes = _normalize_axis(axes, ndim)
    dims = [True if i in axes else False for i in range(ndim)]
    return tf.reverse(x, dims)


def concatenate(tensors, axis=-1):
    axis = _normalize_axis(axis, tensors[0].get_shape().ndims)
    return tf.concat(axis, tensors)


def tile(x, n):
    # TODO: error here
    ndim = x.get_shape().ndims
    return tf.tile(x, [1 for i in range(ndim - 1)] + [n])


def stack(tensors):
    """ (5, 2) and (5, 2) => (2, 5, 2) """
    return tf.pack(tensors)


def expand_dims(x, dim=-1):
    """ Add a 1-sized dimension at index "dim". """
    return tf.expand_dims(x, dim)


def reshape(x, shape):
    """ x.shape = [25, 08, 12]
    reshape(shape=([1], [2], [0]))
    => x.shape = (08, 12, 25)
    """
    input_shape = get_shape(x)
    new_shape = []
    for i in shape:
        if i is None:
            new_shape.append(-1)
        elif isinstance(i, (list, tuple)):
            new_shape.append(input_shape[i[0]])
        else:
            new_shape.append(i)
    new_shape = tuple([-1 if i is None else i
                       for i in new_shape])
    return tf.reshape(x, new_shape)


def dimshuffle(x, pattern):
    """Transpose dimensions.

    pattern should be a tuple or list of
    dimension indices, e.g. [0, 2, 1].
    """
    x = tf.transpose(x, perm=[i for i in pattern if i != 'x'])
    # insert new dimension
    for i, p in enumerate(pattern):
        if p == 'x':
            x = tf.expand_dims(x, i)
    return x


def flatten(x, outdim=1):
    input_shape = x.get_shape().as_list()
    other_shape = tuple([input_shape[i] for i in range(outdim - 1)])
    n = np.prod(input_shape[(outdim - 1):])
    output_shape = [-1 if i is None else i
                    for i in other_shape + (n,)]
    return tf.reshape(x, output_shape)


def repeat(x, n, axes=None):
    """Repeat a N-D tensor.

    If x has shape (s1, s2, s3) and axes=(1, -1), the output
    will have shape (s1, s2 * n[0], s3 * n[1]).
    """
    if axes is not None:
        ndim = x.get_shape().ndims
        if not isinstance(axes, (tuple, list)):
            axes = (axes,)
        axes = _normalize_axis(axes, ndim)
        n = as_tuple(n, len(axes))
        return tf.tile(x, [n[axes.index(i)] if i in axes else 1
                           for i in range(ndim)])
    else:
        return tile(x, n)


def squeeze(x, axis):
    """Remove a 1-dimension from the tensor at index "axis".
    """
    axis = axis % x.get_shape().ndims
    return tf.squeeze(x, [axis])


def pad(x, axes=1, padding=1):
    """Pad the all dimension given in axes` of a N-D tensor
    with "padding" zeros left and right.

    Example
    -------
    >>> X = [[1, 1, 1],
             [1, 1, 1]]
    >>> Y1 = pad(X, axes=1, padding=1)
    >>> Y1 = [[0, 1, 1, 1, 0],
              [0, 1, 1, 1, 0]]
    >>> Y2 = pad(X, axes=(0, 1), padding=1)
    >>> Y2 = [[0, 0, 0, 0, 0],
              [0, 1, 1, 1, 0],
              [0, 1, 1, 1, 0],
              [0, 0, 0, 0, 0]]
    """
    ndim = x.get_shape().ndims
    if not isinstance(axes, (tuple, list)):
        axes = (axes,)
    axes = tuple([i % ndim for i in axes])
    padding = as_tuple(padding, len(axes), int)
    return tf.pad(x, [[padding[axes.index(i)], padding[axes.index(i)]] if i in axes
                      else [0, 0]
                      for i in range(ndim)])


# ===========================================================================
# VALUE MANIPULATION
# ===========================================================================
def get_value(x):
    if isinstance(x, (tuple, list)):
        return get_session().run(x)
    return x.eval(session=get_session())


def set_value(x, value):
    '''Sets the value of a tensor variable,
    from a Numpy array.
    '''
    value = np.asarray(value, dtype=x.dtype.base_dtype)
    if hasattr(x, '_assign_placeholder'):
        assign_placeholder = x._assign_placeholder
        assign_op = x._assign_op
    else:
        assign_placeholder = tf.placeholder(dtype=x.dtype.base_dtype, shape=value.shape)
        assign_op = x.assign(assign_placeholder)
        x._assign_placeholder = assign_placeholder
        x._assign_op = assign_op
    get_session().run(assign_op, feed_dict={assign_placeholder: value})


# ===========================================================================
# Graph manipulation
# ===========================================================================
def gradients(loss, variables, consider_constant=None):
    """
    Return symbolic gradients for one or more variables with respect to some
    cost.

    For more information about how automatic differentiation works in Theano,
    see :mod:`gradient`. For information on how to implement the gradient of
    a certain Op, see :func:`grad`.

    Parameters
    ----------
    cost : scalar (0-dimensional) tensor variable or None
        Value with respect to which we are differentiating.  May be
        `None` if known_grads is provided.
    wrt : variable or list of variables
        term[s] for which we want gradients
    consider_constant : list of expressions(variables)
        expressions not to backpropagate through
    Returns
    -------
    variable or list/tuple of variables (matches `wrt`)
        symbolic expression of gradient of `cost` with respect to each
        of the `wrt` terms.  If an element of `wrt` is not
        differentiable with respect to the output, then a zero
        variable is returned.

    Example
    -------
    >>> # For consider_constant:
    >>> a = T.variable(1.2)
    >>> b = T.variable(1.3)
    >>> x = a * b
    >>>
    >>> y = T.variable(2.)
    >>> z = T.variable(1.)
    >>>
    >>> z_pred = x * y
    >>> loss = T.pow((z - z_pred), 2)
    >>>
    >>> G = T.gradients(loss, [a, b, y], consider_constant=[x])
    >>>
    >>> for g in G:
    >>>     print(g.eval())
    >>> # a_grad=0. b_grad=0. y_grad=6.614
    """
    return tf.gradients(loss, variables=variables,
                        colocate_gradients_with_ops=True)


def stop_gradient(vars):
    return tf.stop_gradient(vars)


def jacobian(loss, variables):
    raise NotImplementedError


def hessian(loss, variables):
    raise NotImplementedError


def Scan(fn,
         sequences=None,
         outputs_info=None,
         n_steps=None,
         truncate_gradient=-1,
         backwards=False,
         name=None):
    """
    Note
    ----
    backwards mode only invert sequences then iterate over them
    """
    return theano.scan(fn,
                       sequences=sequences,
                       outputs_info=outputs_info,
                       non_sequences=None,
                       n_steps=n_steps,
                       truncate_gradient=truncate_gradient,
                       go_backwards=backwards,
                       mode=None,
                       name=name,
                       profile=False,
                       allow_gc=None,
                       strict=False)


class Function(object):
    """ Two way to call this Function
    f(x1, x2, x3)
    or f('x1'=x1, 'x2'=x2, 'x3'=x3)
    """

    def __init__(self, inputs, outputs, updates=[], **kwargs):
        # ====== validate input ====== #
        if isinstance(inputs, dict):
            self.inputs_name = inputs.keys()
            self.inputs = inputs.values()
        elif not isinstance(inputs, (tuple, list)):
            self.inputs = [inputs]
        if not hasattr(self, 'inputs_name'):
            self.inputs_name = [i.name for i in self.inputs]
        # ====== validate outputs ====== #
        return_list = True
        if not isinstance(outputs, (tuple, list)):
            outputs = (outputs,)
            return_list = False
        self.outputs = list(outputs)
        self.return_list = return_list
        # ====== validate updates ====== #
        if not isinstance(updates, OrderedDict):
            updates = OrderedDict(updates)
        updates = dict_union(updates, ComputationGraph(outputs).updates)
        updates = updates.items()
        # create updates ops
        with tf.control_dependencies(self.outputs):
            updates_ops = []
            for update in updates:
                if isinstance(update, (tuple, list)):
                    p, new_p = update
                    updates_ops.append(tf.assign(p, new_p))
                else:
                    # assumed already an op
                    updates_ops.append(update)
            self.updates_op = tf.group(*updates_ops)

    def __call__(self, *inputs, **kwargs):
        # dictionary as inputs
        if len(kwargs) == len(self.inputs_name):
            inputs = [kwargs[i] for i in self.inputs_name]
        # ====== create feed_dict ====== #
        feed_dict = {}
        for tensor, value in zip(self.inputs, inputs):
            feed_dict[tensor] = value
        # ====== run the output ====== #
        session = get_session()
        updated = session.run(self.outputs + [self.updates_op],
                              feed_dict=feed_dict)
        # ====== get the results ====== #
        outputs = updated[:len(self.outputs)]
        if not self.return_list:
            outputs = outputs[0]
        return outputs


# ===========================================================================
# utilities
# ===========================================================================
def one_hot(x, nb_class):
    '''Input: nD integer tensor of shape (batch_size, dim1, dim2, ... dim(n-1))
    Output: (n + 1)D one hot representation of the input
    with shape (batch_size, dim1, dim2, ... dim(n-1), nb_classes)
    '''
    return tf.one_hot(x, depth=nb_class, axis=-1)


def confusion_matrix(y_pred, y_true, labels=None):
    """
    Computes the confusion matrix of given vectors containing
    actual observations and predicted observations.
    Parameters
    ----------
    pred : 1-d or 2-d tensor variable
    actual : 1-d or 2-d tensor variable
    labels : array, shape = [n_classes], optional
        List of labels to index the matrix. This may be used to reorder
        or select a subset of labels.
        If none is given, those that appear at least once
        in ``y_true`` or ``y_pred`` are used in sorted order.

    """
    from tensorflow.contrib.metrics import confusion_matrix
    if y_true.get_shape().ndims == 2:
        y_true = tf.argmax(y_true, -1)
    elif y_true.get_shape().ndims != 1:
        raise ValueError('actual must be 1-d or 2-d tensor variable')

    if y_pred.get_shape().ndims == 2:
        y_pred = tf.argmax(y_pred, axis=-1)
    elif y_pred.get_shape().ndims != 1:
        raise ValueError('pred must be 1-d or 2-d tensor variable')

    return confusion_matrix(y_pred, y_true,
                            num_classes=None if labels is None else len(labels))


def one_hot_max(x, axis=-1):
    """
    Example
    -------
    >>> Input: [[0.0, 0.0, 0.5],
    >>>         [0.0, 0.3, 0.1],
    >>>         [0.6, 0.0, 0.2]]
    >>> Output: [[0.0, 0.0, 1.0],
    >>>         [0.0, 1.0, 0.0],
    >>>         [1.0, 0.0, 0.0]]
    """
    dtype = x.dtype.base_dtype
    return tf.cast(
        tf.equal(tf.cast(arange(x.get_shape()[axis])[None, :], 'int32'),
                 tf.cast(argmax(x, axis=axis, keepdims=True), 'int32')
                ),
        dtype
    )


def apply_mask(x, mask):
    """
    x : 3D tensor
    mask : 2D tensor

    Example
    -------
    >>> Input: [128, 500, 120]
    >>> Mask:  [1, 1, 0]
    >>> Output: [128, 500, 0]
    """
    return tf.mul(x, tf.expand_dims(mask, -1))


# ===========================================================================
# RANDOMNESS
# ===========================================================================
_RNG = np.random.RandomState(seed=CONFIG['seed'])


def set_rng(seed):
    global _RNG
    _RNG = np.random.RandomState(seed=seed)


def random_normal(shape, mean=0.0, std=1.0, dtype=FLOATX):
    return tf.random_normal(shape, mean=mean, stddev=std,
                            dtype=dtype.base_dtype if hasattr(dtype, 'base_dtype') else dtype,
                            seed=_RNG.randint(10e6))


def random_uniform(shape, low=0.0, high=1.0, dtype=FLOATX):
    return tf.random_uniform(shape, minval=low, maxval=high,
                             dtype=dtype.base_dtype if hasattr(dtype, 'base_dtype') else dtype,
                             seed=_RNG.randint(10e6))


def random_binomial(shape, p, dtype=FLOATX, seed=None):
    if hasattr(dtype, 'base_dtype'):
        dtype = dtype.base_dtype
    return tf.select(tf.random_uniform(shape, dtype=dtype, seed=_RNG.randint(10e6)) <= p,
                     tf.ones(shape, dtype=dtype),
                     tf.zeros(shape, dtype=dtype))