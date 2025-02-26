# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Manual collectives which use bidirectional ICI and fully overlap compute."""

from typing import Optional, Tuple

import jax
from jax import lax
import jax.numpy as jnp
import jax.scipy

# scatter_dimension decoder ring:
#   matmul_reducescatter:
#     * scatter_dimension[0] indexes into rhs, is the sharded axis, must be a
#       contracting dimension
#     * scatter_dimension[1] indexes into output, is the sharded axis, must
#       be an output channel
#     * subsplit_axis
#   allgather_matmul:
#     * gather_dimension[0] indexes into rhs, is an lhs-sharded/rhs-unsharded
#       axis, must be an input channel
#     * gather_dimension[1] is unused


# pylint:disable = unused-argument
def matmul_reducescatter_no_collective(einsum_spec,
                                       lhs,
                                       rhs,
                                       scatter_dimension,
                                       axis_name,
                                       layer,
                                       subsplit_axis,
                                       layer_axis=0):
  """Non overlapped reduce scatter using default psum."""
  del subsplit_axis  # Only for bidirectional_throughput
  rhs = lax.dynamic_index_in_dim(rhs, layer, layer_axis, keepdims=False)
  tmp = jnp.einsum(einsum_spec, lhs, rhs)

  result = lax.psum_scatter(
      tmp, axis_name, scatter_dimension=scatter_dimension[1], tiled=True)

  return result


def dynamic_index_and_slice(index_axis, index, slice_axis,
                            slice_start, slice_length,
                            x):
  """Does multi axis slicing."""
  assert index_axis != slice_axis, f'{index_axis} != {slice_axis}'
  sizes = list(x.shape)
  starts = [0] * len(sizes)
  starts[index_axis] = index
  starts[slice_axis] = slice_start
  sizes[index_axis] = 1
  sizes[slice_axis] = slice_length
  x = lax.dynamic_slice(x, starts, sizes)
  x = lax.squeeze(x, [index_axis])
  return x


def matmul_reducescatter_oneway(einsum_spec,
                                lhs,
                                rhs,
                                scatter_dimension,
                                axis_name,
                                layer,
                                subsplit_axis,
                                layer_axis=0):
  """Uses a single ICI direction, overlapped weight stationary reduce scatter.

  Args:
    einsum_spec: Spec for the einsum
    lhs: Typically activations
    rhs: Typically weights
    scatter_dimension: The first argument denotes the logical axis along which
      the output will be scattered, the second is unused. TODO(sholto): Make
      string parse
    axis_name: The hardware axis along which we are reducing
    layer: Weights are stored with layer as the first dimension, index of the
      layer
    subsplit_axis: Unused, for easy compatibility with other layerdefs
    layer_axis: Which axis is the layer dimension

  Returns:
    Result of a matmul and reduce_scatter
  """
  del subsplit_axis  # Only for bidirectional_throughput
  # [batch, maxlen, dmodel.X] @ [heads.YZ, dmodel.X, q_wi_per_head]
  # -> (matmul)
  # -> [batch, maxlen, heads.YZ, q_wi_per_head]{X unreduced}
  # -> (reducescatter over X into heads)
  # -> [batch, maxlen, heads.YZX, q_wi_per_head]
  # we want to maintain an accumulator, then permute and add to said accumulator
  axis_size = lax.psum(1, axis_name)
  axis_index = lax.axis_index(axis_name)
  rhs_scatter_axis = scatter_dimension[0]
  if rhs_scatter_axis >= layer_axis:
    rhs_scatter_axis += 1

  permutes_remaining = axis_size - 1

  chunk_index = (axis_index + permutes_remaining) % axis_size
  chunk_size = rhs.shape[rhs_scatter_axis] // axis_size
  first_chunk = dynamic_index_and_slice(layer_axis, layer, rhs_scatter_axis,
                                        chunk_index * chunk_size, chunk_size,
                                        rhs)

  p = jnp.einsum(einsum_spec, lhs, first_chunk)
  accum = jnp.zeros(p.shape, dtype=jnp.bfloat16)

  # collective_matmul reduce scatter is as follows:
  # you chunk along a different dimension to the partitioned one you
  # are multiplying along
  # to reduce, you sum chunk partial sums per index, therefore you get
  # chunk sized full sums partitioned along the scatter axis
  def collective_matmul(i, carrys):
    permutes_remaining_after_einsum = (axis_size - 1) - i
    chunk_index = (axis_index + permutes_remaining_after_einsum) % axis_size
    # matmul
    accum, p = carrys
    accum = accum + p
    c = dynamic_index_and_slice(layer_axis, layer, rhs_scatter_axis,
                                chunk_index * chunk_size, chunk_size, rhs)
    p = jnp.einsum(einsum_spec, lhs, c)
    accum = lax.ppermute(
        accum,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])

    return accum, p

  accum, p = jax.lax.fori_loop(1, axis_size, collective_matmul, (accum, p))

  return accum + p


def plain_reducescatter(val,
                        scatter_dimension,
                        axis_name,
                        subsplit_axis=None):
  return lax.psum_scatter(
      val, axis_name, scatter_dimension=scatter_dimension, tiled=True)


def reducescatter_oneway(val,
                         scatter_dimension,
                         axis_name,
                         subsplit_axis=None):
  """Uses one ICI direction, overlapped weight stationary reduce scatter."""
  # [A, B, C] -> [A, B, C.X]
  axis_size = lax.psum(1, axis_name)
  axis_index = lax.axis_index(axis_name)
  scatter_axis = scatter_dimension

  permutes_remaining = axis_size - 1

  chunk_size = val.shape[scatter_axis] // axis_size
  p = lax.dynamic_slice_in_dim(val,
                               (axis_index + permutes_remaining) % axis_size,
                               chunk_size, scatter_axis)

  accum = jnp.zeros(p.shape, dtype=jnp.bfloat16)

  def collective(i, carrys):
    permutes_remaining_after = (axis_size - 1) - i
    chunk_index = (axis_index + permutes_remaining_after) % axis_size

    accum, p = carrys
    accum = accum + p

    p = lax.dynamic_slice_in_dim(val, chunk_index * chunk_size, chunk_size,
                                 scatter_axis)
    accum = lax.ppermute(
        accum,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])

    return accum, p

  accum, p = jax.lax.fori_loop(1, axis_size, collective, (accum, p))

  return accum + p


def reducescatter_bidirectional_throughput(val, scatter_dimension,
                                           axis_name,
                                           subsplit_axis):
  """Using two ICI directions, manual reduce scatter."""
  axis_size = lax.psum(1, axis_name)

  chunk_size = val.shape[scatter_dimension] // axis_size

  chunk_index = 0

  p = lax.dynamic_slice_in_dim(val, chunk_index * chunk_size, chunk_size,
                               scatter_dimension)

  accum_shape = list(p.shape)
  accum_shape[subsplit_axis] = accum_shape[subsplit_axis] // 2
  accum_pos = jnp.zeros(accum_shape, dtype=jnp.bfloat16)
  accum_neg = jnp.zeros(accum_shape, dtype=jnp.bfloat16)

  # collective_matmul reduce scatter is as follows:
  # you chunk along a different dimension to the partitioned one you
  # are multiplying along
  # to reduce, you sum chunk partial sums per index, therefore you get
  # chunk sized full sums partitioned along the scatter axis
  def collective(i, carrys):
    # matmul
    accum_pos, accum_neg, p = carrys
    p_pos, p_neg = jnp.split(p, 2, subsplit_axis)
    accum_pos += p_pos
    accum_neg += p_neg

    p = lax.dynamic_slice_in_dim(val, chunk_index * chunk_size, chunk_size,
                                 scatter_dimension)

    accum_pos = lax.ppermute(
        accum_pos,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])
    accum_neg = lax.ppermute(
        accum_neg,
        axis_name,
        perm=[(j, (j - 1) % axis_size) for j in range(axis_size)])

    return accum_pos, accum_neg, p

  with jax.disable_jit():
    accum_pos, accum_neg, p = jax.lax.fori_loop(1, axis_size, collective,
                                                (accum_pos, accum_neg, p))

  return jnp.concatenate((accum_pos, accum_neg), subsplit_axis) + p


def reducescatter_bidirectional_latency(val,
                                        scatter_dimension,
                                        axis_name,
                                        subsplit_axis=None):
  """Using two ICI directions, manual reduce scatter."""

  axis_size = lax.psum(1, axis_name)  # two for both directions

  matmul_steps = axis_size // 2
  chunk_size = val.shape[scatter_dimension] // matmul_steps
  chunk_index = 0
  p = lax.dynamic_slice_in_dim(val, chunk_index * chunk_size, chunk_size,
                               scatter_dimension)

  p_pos, p_neg = jnp.split(p, 2, scatter_dimension)
  accum_size = p_pos.shape
  accum_pos = jnp.zeros(accum_size, dtype=jnp.bfloat16)
  accum_neg = jnp.zeros(accum_size, dtype=jnp.bfloat16)

  # collective_matmul reduce scatter is as follows:
  # you chunk along a different dimension to the partitioned one you
  # are multiplying along
  # to reduce, you sum chunk partial sums per index, therefore you get
  # chunk sized full sums partitioned along the scatter axis
  def collective_matmul(i, carrys):
    # matmul
    accum_pos, accum_neg, p = carrys
    p_pos, p_neg = jnp.split(p, 2, scatter_dimension)
    accum_pos += p_pos
    accum_neg += p_neg

    p = lax.dynamic_slice_in_dim(val, chunk_index * chunk_size, chunk_size,
                                 scatter_dimension)
    accum_pos = lax.ppermute(
        accum_pos,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])
    accum_neg = lax.ppermute(
        accum_neg,
        axis_name,
        perm=[(j, (j - 1) % axis_size) for j in range(axis_size)])

    return accum_pos, accum_neg, p

  accum_pos, accum_neg, p = jax.lax.fori_loop(1, matmul_steps,
                                              collective_matmul,
                                              (accum_pos, accum_neg, p))

  p_pos, p_neg = jnp.split(p, 2, scatter_dimension)
  accum_pos = lax.ppermute(
      accum_pos + p_pos,
      axis_name,
      perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])

  return accum_pos + accum_neg + p_neg


def async_matmul_allgather_one_way(einsum_spec,
                                   lhs,
                                   rhs,
                                   gather_dimension,
                                   axis_name,
                                   layer,
                                   layer_axis=0,
                                   subsplit_axis=None):
  """Uses a single ICI direction, overlapped all gather -> matmul."""
  # [batch, maxlen, heads.YZX, o_wo_per_head]
  #         @ [heads.YZ, o_wo_per_head, dmodel.X]
  # allgather LHS over X: List([batch, maxlen, heads.YZ/X, o_wo_per_head] * X)
  # split RHS over heads by X: List([heads.YZ/X, o_wo_per_head, dmodel.X ]) * X
  # -> (matmul) X times, overlap with compute
  # -> X partial sums [batch, maxlen, dmodel.X]{YZ unreduced} -> sum
  # -> Later on: (unfused reducescatter)
  # -> [batch, maxlen, dmodel.XYZ]
  axis_size = lax.psum(1, axis_name)  # along X
  axis_index = lax.axis_index(axis_name)
  split_axis = gather_dimension[0]
  chunk_size = rhs.shape[split_axis] // axis_size

  first_chunk = lax.dynamic_index_in_dim(rhs, layer, layer_axis, keepdims=False)

  # print(f'first_chunk: {first_chunk.shape}, rhs: {rhs.shape}')
  chunk_size = first_chunk.shape[split_axis] // axis_size

  if split_axis >= layer_axis:
    split_axis += 1

  def indexed_computation(i, lhs):
    permutes_remaining_after_einsum = (axis_size - 1) - i
    chunk_index = (axis_index + permutes_remaining_after_einsum) % axis_size
    c = dynamic_index_and_slice(layer_axis, layer, split_axis,
                                chunk_index * chunk_size, chunk_size, rhs)
    p = jnp.einsum(einsum_spec, lhs, c)
    return p

  accum_shape = jax.eval_shape(indexed_computation, 0, lhs)
  accum = jnp.zeros(accum_shape.shape, dtype=jnp.bfloat16)

  # all gather collective_matmul is as follows:
  # you chunk along a dimension of the weights, you then shift the acts around
  # and multiply that with it's corresponding index in the weights, and sum
  # partials
  def collective_matmul(i, carrys):

    # matmul
    accum, lhs = carrys

    p = indexed_computation(i, lhs)
    # in parallel, we shift the lhs around the next one
    lhs = lax.ppermute(
        lhs,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])

    accum = accum + p

    return accum, lhs

  accum, lhs = jax.lax.fori_loop(0, axis_size - 1, collective_matmul,
                                 (accum, lhs))

  i = axis_size - 1
  p = indexed_computation(i, lhs)

  return accum + p


def async_matmul_allgather_throughput(
    einsum_spec,
    lhs,
    rhs,
    gather_dimension,
    axis_name,
    layer,
    subsplit_axis,
    layer_axis=0,
):
  """Uses a two ICI directions, overlapped."""
  # [batch, maxlen, heads.YZX, o_wo_per_head]
  #           @ [heads.YZ, o_wo_per_head, dmodel.X]
  # allgather LHS over X: List([batch, maxlen, heads.YZ/X, o_wo_per_head] * X)
  # split RHS over heads by X: List([heads.YZ/X, o_wo_per_head, dmodel.X ]) * X
  # -> ('bthd,hde->bte') X times, overlap with compute
  # -> X partial sums [[batch, maxlen, dmodel.X]{YZ unreduced}] -> sum
  # -> Later on: (unfused reducescatter)
  # -> [batch, maxlen, dmodel.XYZ]
  axis_size = lax.psum(1, axis_name)  # along X
  axis_index = lax.axis_index(axis_name)
  split_axis = gather_dimension[0]
  chunk_size = rhs.shape[split_axis] // axis_size

  first_chunk = lax.dynamic_index_in_dim(rhs, layer, layer_axis, keepdims=False)

  chunk_size = first_chunk.shape[split_axis] // axis_size

  if split_axis >= layer_axis:
    split_axis += 1

  def indexed_computation(i, lhs):
    permutes_remaining_after_einsum = (axis_size - 1) - i
    chunk_index = (axis_index + permutes_remaining_after_einsum) % axis_size
    c = dynamic_index_and_slice(layer_axis, layer, split_axis,
                                chunk_index * chunk_size, chunk_size, rhs)
    p = jnp.einsum(einsum_spec, lhs, c)
    return p

  accum_shape = jax.eval_shape(indexed_computation, 0, lhs)
  accum = jnp.zeros(accum_shape.shape, dtype=jnp.bfloat16)
  lhs_top, lhs_bottom = jnp.split(lhs, 2, subsplit_axis)

  # all gather collective_matmul is as follows:
  # you chunk along a dimension of the weights, you then shift the acts around
  # and multiply that with it's corresponding index in the weights, and sum
  # partials
  def collective_matmul(i, carrys):

    # matmul
    accum, lhs_top, lhs_bottom = carrys

    lhs = jnp.concatenate([lhs_top, lhs_bottom], axis=subsplit_axis)
    p = indexed_computation(i, lhs)

    # in parallel, we shift the lhs around the next one
    lhs_top = lax.ppermute(
        lhs_top,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])
    lhs_bottom = lax.ppermute(
        lhs_bottom,
        axis_name,
        perm=[(j, (j - 1) % axis_size) for j in range(axis_size)])

    accum = accum + p

    return accum, lhs_top, lhs_bottom

  accum, lhs_top, lhs_bottom = jax.lax.fori_loop(0, axis_size - 1,
                                                 collective_matmul,
                                                 (accum, lhs_top, lhs_bottom))

  i = axis_size - 1
  lhs = jnp.concatenate([lhs_top, lhs_bottom], axis=subsplit_axis)
  p = indexed_computation(i, lhs)

  return accum + p


# subsplit_axis: lhs contracting dimension, along which we concatenate + and -
#   directions
# gather_dimension[0]: rhs contracting dimension, along which we split for steps
# gather_dimension[1]: unused.
def async_matmul_allgather_latency(
    einsum_spec,
    lhs,
    rhs,
    gather_dimension,
    axis_name,
    layer,
    subsplit_axis,
    layer_axis=0,
):
  """Uses a two ICI directions, overlapped, half the steps, double the mem."""
  # [batch, maxlen, heads.YZX, o_wo_per_head]
  #                                   @ [heads.YZ, o_wo_per_head, dmodel.X]
  # first allgather the next shard so that we can multiply with the whole thing
  # gives
  # allgather over X: List([batch, maxlen, 2 * heads.YZ/X, o_wo_per_head] * X)
  # split RHS over heads by X: List([heads.YZ/X, o_wo_per_head, dmodel.X ]) * X
  # -> ('bthd,hde->bte') X times, overlap with compute
  # -> X partial sums [[batch, maxlen, dmodel.X]{YZ unreduced}] -> sum
  # -> Later on: (unfused reducescatter)
  # -> [batch, maxlen, dmodel.XYZ]
  axis_size = lax.psum(1, axis_name)  # along X
  axis_index = lax.axis_index(axis_name)
  matmul_steps = axis_size // 2

  split_axis = gather_dimension[0]
  chunk_size = rhs.shape[split_axis] // axis_size

  first_chunk = lax.dynamic_index_in_dim(rhs, layer, layer_axis, keepdims=False)

  chunk_size = first_chunk.shape[split_axis] // matmul_steps

  if split_axis >= layer_axis:
    split_axis += 1

  def indexed_computation(i, lhs):
    permutes_remaining_after_einsum = (axis_size - 1) - i
    chunk_index = (axis_index + permutes_remaining_after_einsum) % axis_size
    c = dynamic_index_and_slice(layer_axis, layer, split_axis,
                                chunk_index * chunk_size, chunk_size, rhs)
    p = jnp.einsum(einsum_spec, lhs, c)
    return p

  # get the current and next lhs on the same device
  lhs_bwd = lhs
  lhs_fwd = lax.ppermute(
      lhs, axis_name, perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])

  working_lhs = jnp.concatenate([lhs_fwd, lhs_bwd], axis=subsplit_axis)
  accum_shape = jax.eval_shape(indexed_computation, 0, working_lhs)
  accum = jnp.zeros(accum_shape.shape, dtype=jnp.bfloat16)

  def collective_matmul(i, carrys):
    accum, lhs_fwd, lhs_bwd = carrys

    lhs = jnp.concatenate([lhs_fwd, lhs_bwd], axis=subsplit_axis)
    p = indexed_computation(i, lhs)

    lhs_fwd = lax.ppermute(
        lhs_fwd,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])
    lhs_bwd = lax.ppermute(
        lhs_bwd,
        axis_name,
        perm=[(j, (j - 1) % axis_size) for j in range(axis_size)])

    accum = accum + p

    return accum, lhs_fwd, lhs_bwd

  accum, lhs_fwd, lhs_bwd = jax.lax.fori_loop(0, matmul_steps - 1,
                                              collective_matmul,
                                              (accum, lhs_fwd, lhs_bwd))

  i = axis_size - 1
  lhs = jnp.concatenate([lhs_fwd, lhs_bwd], axis=subsplit_axis)
  p = indexed_computation(i, lhs)

  return accum + p


# bidirectional form
def matmul_reducescatter_bidirectional_throughput(einsum_spec,
                                                  lhs,
                                                  rhs,
                                                  scatter_dimension,
                                                  axis_name,
                                                  layer,
                                                  subsplit_axis,
                                                  layer_axis=0):
  """Uses a two ICI directions, overlapped."""
  # [batch, maxlen, dmodel.X] @ [heads.YZ, dmodel.X, q_wi_per_head]
  # -> (matmul)
  # -> [batch, maxlen, heads.YZ, q_wi_per_head]{X unreduced}
  # -> (reducescatter over X into heads)
  # -> [batch, maxlen, heads.YZX, q_wi_per_head]
  # we want to maintain an accumulator, then permute and add to said accumulator
  axis_size = lax.psum(1, axis_name)  # two for both directions
  rhs_scatter_axis, _ = scatter_dimension
  if rhs_scatter_axis >= layer_axis:
    rhs_scatter_axis += 1

  chunk_size = rhs.shape[rhs_scatter_axis] // axis_size
  chunk_index = 0
  first_chunk = dynamic_index_and_slice(layer_axis, layer, rhs_scatter_axis,
                                        chunk_index * chunk_size, chunk_size,
                                        rhs)
  # print(f'lhs {lhs.shape} rhs {rhs.shape}, first_chunk {first_chunk.shape}')
  p = jnp.einsum(einsum_spec, lhs, first_chunk)
  # print(f'{einsum_spec}, {p.shape}')
  accum_shape = list(p.shape)
  accum_shape[subsplit_axis] = accum_shape[subsplit_axis] // 2
  accum_pos = jnp.zeros(accum_shape, dtype=jnp.bfloat16)
  accum_neg = jnp.zeros(accum_shape, dtype=jnp.bfloat16)

  # collective_matmul reduce scatter is as follows:
  # you chunk along a different dimension to the partitioned one you
  # are multiplying along
  # to reduce, you sum chunk partial sums per index, therefore you get
  # chunk sized full sums partitioned along the scatter axis
  def collective_matmul(i, carrys):
    # matmul
    accum_pos, accum_neg, p = carrys
    # print(p.shape, subsplit_axis)
    p_pos, p_neg = jnp.split(p, 2, subsplit_axis)
    accum_pos += p_pos
    accum_neg += p_neg

    # In principle we want:
    #   permutes_remaining_after_einsum = (axis_size - 1) - i
    #   chunk_index_pos = (axis_index + permutes_remaining) % axis_size
    #   chunk_index_neg = (axis_index - permutes_remaining) % axis_size
    # But this has the disadvantage of needing to slice differently for
    # the positive and negative directions, which in turn forces us to
    # split the einsum into two separate einsums, harming efficiency.
    # Instead, we (imagine that we) pre-shuffle the weight matrix in the
    # output channels dimension so that indexing by 'i' on every chip is
    # correct.
    c = dynamic_index_and_slice(layer_axis, layer, rhs_scatter_axis,
                                i * chunk_size, chunk_size, rhs)
    p = jnp.einsum(einsum_spec, lhs, c)

    accum_pos = lax.ppermute(
        accum_pos,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])
    accum_neg = lax.ppermute(
        accum_neg,
        axis_name,
        perm=[(j, (j - 1) % axis_size) for j in range(axis_size)])

    return accum_pos, accum_neg, p

  accum_pos, accum_neg, p = jax.lax.fori_loop(1, axis_size, collective_matmul,
                                              (accum_pos, accum_neg, p))

  result = jnp.concatenate((accum_pos, accum_neg), subsplit_axis) + p
  return result


def matmul_reducescatter_bidirectional_latency(einsum_spec,
                                               lhs,
                                               rhs,
                                               scatter_dimension,
                                               axis_name,
                                               layer,
                                               subsplit_axis,
                                               layer_axis=0):
  """Uses a two ICI directions, overlapped, half the steps."""
  # [batch, maxlen, dmodel.X] @ [heads.YZ, dmodel.X, q_wi_per_head]
  # -> (matmul)
  # -> [batch, maxlen, heads.YZ, q_wi_per_head]{X unreduced}
  # -> (reducescatter over X into heads)
  # -> [batch, maxlen, heads.YZX, q_wi_per_head]
  # we want to maintain an accumulator, then permute and add to said accumulator
  axis_size = lax.psum(1, axis_name)  # two for both directions
  scatter_axis = scatter_dimension[0]

  if scatter_axis >= layer_axis:
    scatter_axis += 1
  matmul_steps = axis_size // 2

  chunk_size = rhs.shape[scatter_axis] // matmul_steps
  chunk_index = 0
  first_chunk = dynamic_index_and_slice(layer_axis, layer, scatter_axis,
                                        chunk_index * chunk_size, chunk_size,
                                        rhs)

  if chunk_size == 0:
    raise ValueError('something is bad')
  elif chunk_size == 1:
    pass  # leave subsplit_axis alone
  else:
    subsplit_axis = scatter_dimension[1]

  p = jnp.einsum(einsum_spec, lhs, first_chunk)
  # TODO(sholto): Is there a way to use eval shape if dynamic?
  p_pos, p_neg = jnp.split(p, 2, subsplit_axis)
  accum_size = p_pos.shape
  accum_pos = jnp.zeros(accum_size, dtype=jnp.bfloat16)
  accum_neg = jnp.zeros(accum_size, dtype=jnp.bfloat16)

  # collective_matmul reduce scatter is as follows:
  # you chunk along a different dimension to the partitioned one you
  # are multiplying along
  # to reduce, you sum chunk partial sums per index, therefore you get
  # chunk sized full sums partitioned along the scatter axis
  def collective_matmul(i, carrys):
    # matmul
    accum_pos, accum_neg, p = carrys
    p_pos, p_neg = jnp.split(p, 2, subsplit_axis)
    accum_pos += p_pos
    accum_neg += p_neg

    # In principle we want:
    #   permutes_remaining_pos = matmul_steps - i
    #   permutes_remaining_neg = permutes_remaining_pos - 1
    #   chunk_index_pos = (axis_index + permutes_remaining_pos) % matmul_steps
    #   chunk_index_neg = (axis_index - permutes_remaining_neg) % matmul_steps
    # But this has the disadvantage of needing to slice differently for
    # the positive and negative directions, which in turn forces us to
    # split the einsum into two separate einsums, harming efficiency.
    # Instead, we (imagine that we) pre-shuffle the weight matrix in the
    # output channels dimension so that indexing by 'i' on every chip is
    # correct.

    c = dynamic_index_and_slice(layer_axis, layer, scatter_axis, i * chunk_size,
                                chunk_size, rhs)
    p = jnp.einsum(einsum_spec, lhs, c)
    accum_pos = lax.ppermute(
        accum_pos,
        axis_name,
        perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])
    accum_neg = lax.ppermute(
        accum_neg,
        axis_name,
        perm=[(j, (j - 1) % axis_size) for j in range(axis_size)])

    return accum_pos, accum_neg, p

  accum_pos, accum_neg, p = jax.lax.fori_loop(1, matmul_steps,
                                              collective_matmul,
                                              (accum_pos, accum_neg, p))

  p_pos, p_neg = jnp.split(p, 2, subsplit_axis)
  accum_pos = lax.ppermute(
      accum_pos + p_pos,
      axis_name,
      perm=[(j, (j + 1) % axis_size) for j in range(axis_size)])

  return accum_pos + accum_neg + p_neg


def matmul_collective_weights_gather_q_wi(
    einsum_spec,
    lhs,
    rhs,
    scatter_dimension,
    axis_name,  # what we are partitioning the chunked axis along
    layer,
    subsplit_axis,
    layer_axis=0):
  """Designed for prefill, moves the weights through x,y and z instead of lhs.
  """
  del subsplit_axis  # Only for bidirectional_throughput
  # [batch.XYZ, t, e] @ [heads.YZ, e.X, q_wi_per_head]
  # result = zeros((Y, Z, ...))
  # for y in Y:
  #   for z in Z:
  #     local_accum = zeros(...)
  #     for x in X:
  #       next_weights = ppermute(weights, 'x', plus_1)
  #       local_accum += jnp.einsum(lhs, next_weights)
  #     result[Y, Z] = local_accum

  x_size, y_size, z_size = lax.psum(1, 'x'), lax.psum(1, 'y'), lax.psum(1, 'z')
  axis_size = lax.psum(1, axis_name)
  axis_index = lax.axis_index(axis_name)
  split_axis = scatter_dimension[0]

  # chunking specific
  chunk_size = lhs.shape[split_axis] // axis_size

  # print(rhs.shape, split_axis, axis_size)

  # print(lhs.shape, rhs.shape, x_size, y_size, z_size, chunk_size)

  # slice into the weights along the axis we will scatter along
  # all others are just in a chain as parititoned
  def indexed_computation(i, rhs):
    permutes_remaining_after_einsum = (axis_size - 1) - i
    chunk_index = (axis_index + permutes_remaining_after_einsum) % axis_size
    # print(lhs.shape,)
    c = jax.lax.dynamic_slice_in_dim(lhs, chunk_index * chunk_size, chunk_size,
                                     split_axis)
    p = jnp.einsum(einsum_spec, c, rhs)
    return p

  # this should be sized as though normally partitoned by the einsum,
  # but also partitioned along the chunk_split axis
  # [b.YZ, t, h.YZ, q]
  # print(lhs.shape, rhs.shape)
  local_accum_shape = jax.eval_shape(indexed_computation, 0, rhs).shape
  # we will concatenate in the loops over heads, so that we are sharded
  # only over batch
  head_idx = 2
  final_heads_dim = local_accum_shape[head_idx] * y_size * z_size
  final_accum_shape = list(local_accum_shape)
  final_accum_shape[head_idx] = final_heads_dim
  final_accum = jnp.zeros(final_accum_shape, dtype=jnp.bfloat16)

  def indexed_update(y, z, update, final_accum):
    """Stacks alongs the dimension sharded by yz."""
    # print(final_accum.shape, update.shape, final_heads_dim)
    indices = [0] * len(final_accum.shape)
    indices[head_idx] = y * z_size + z
    return jax.lax.dynamic_update_slice(final_accum, update, indices)

  # overlap chunk computation with x
  def x_loop_and_matmul(x_i, carrys):
    with jax.named_scope('x'):

      accum, rhs = carrys

      p = indexed_computation(x_i, rhs)
      accum += p

      next_rhs = lax.ppermute(
          rhs, 'x', perm=[(j, (j + 1) % x_size) for j in range(x_size)])

      return (accum, next_rhs)

  def collect_x(final_accum, rhs, z_i, y_i):
    # [b.YZ, t, h.YZ, q]
    local_accum = jnp.zeros(local_accum_shape, dtype=jnp.bfloat16)
    local_accum, rhs = jax.lax.fori_loop(0, x_size - 1, x_loop_and_matmul,
                                         (local_accum, rhs))
    # do one less loop to skip an uneeded permute

    local_accum += indexed_computation(x_size - 1, rhs)
    # concatenate and slowly unshard by heads
    final_accum = indexed_update(y_i, z_i, local_accum, final_accum)
    return final_accum, rhs

  # overlap z movement with entire x computation
  def z_loop(z_i, carrys):
    with jax.named_scope('z'):
      final_accum, rhs, y_i = carrys

      final_accum, rhs = collect_x(final_accum, rhs, z_i, y_i)

      # ideally don't want to do this y extra times
      next_rhs = lax.ppermute(
          rhs, 'z', perm=[(j, (j + 1) % z_size) for j in range(z_size)])

      return (final_accum, next_rhs, y_i)

  # overlap y movement with all z computation
  def y_loop(y_i, carrys):
    with jax.named_scope('y'):
      final_accum, rhs = carrys

      final_accum, rhs, _ = jax.lax.fori_loop(0, z_size - 1, z_loop,
                                              (final_accum, rhs, y_i))
      # we do the z loop one less time than we need to, as the weights
      # shift Z-1 times, then we do an x loop with the final set of z weights
      # [b.YZ, t, h.YZ, q]
      final_accum, rhs = collect_x(final_accum, rhs, z_size - 1, y_i)

      # not so costly to do this one extra time
      next_rhs = lax.ppermute(
          rhs, 'y', perm=[(j, (j + 1) % y_size) for j in range(y_size)])

      return (final_accum, next_rhs)

  final_accum, rhs = jax.lax.fori_loop(0, y_size - 1, y_loop,
                                       (final_accum, rhs))
  # we do one less y loop than we need, and instead finish off with the z loop
  # that would have been (to avoid a final permute)
  final_accum, rhs, _ = jax.lax.fori_loop(0, z_size - 1, z_loop,
                                          (final_accum, rhs, y_size - 1))

  # and also for x
  final_accum, rhs = collect_x(final_accum, rhs, z_size - 1, y_size - 1)

  return final_accum


# pylint: disable = g-doc-args
# pylint: disable = g-doc-return-or-yield
# TODO(sholto): new explaination with new set of collectives
def matmul_collective_weights_gather_o_wo(
    einsum_spec,
    lhs,
    rhs,
    scatter_dimension,
    axis_name,  # what we are partitioning the chunked axis along
    layer,
    subsplit_axis,
    layer_axis=0):
  """Designed for prefill, moves the weights through x,y and z instead of lhs.

  result = zeros((X, ...)) for x in X:

    local_accum = zeros(...)
    for y in Y:
      for z in Z:
        next_weights = ppermute(weights, 'x', plus_1)
        local_accum += jnp.einsum(lhs, next_weights)
    result[X] = local_accum
  """
  del subsplit_axis  # Only for bidirectional_throughput
  del axis_name  # we use both y and z here
  x_size, y_size, z_size = lax.psum(1, 'x'), lax.psum(1, 'y'), lax.psum(1, 'z')
  axis_size = y_size * z_size
  axis_index = lax.axis_index('y') * z_size + lax.axis_index('z')
  split_axis = scatter_dimension[0]

  # chunking specific - should be nh dim on lhs
  chunk_size = lhs.shape[split_axis] // axis_size

  # print('------OWO---------')
  # print('rhs', rhs.shape, 'split', split_axis, 'axis_size', axis_size)

  # print('lhs', lhs.shape, x_size, y_size, z_size, chunk_size)

  # slice into the weights along the axis we will scatter along
  # all others are just in a chain as parititoned
  def indexed_computation(y_i, z_i, rhs):
    permutes_remaining_after_einsum = (axis_size - 1) - (y_i * z_size + z_i)
    chunk_index = (axis_index + permutes_remaining_after_einsum) % axis_size

    c = jax.lax.dynamic_slice_in_dim(lhs, chunk_index * chunk_size, chunk_size,
                                     split_axis)

    p = jnp.einsum(einsum_spec, c, rhs)
    return p

  # this should be sized as though normally partitoned by the einsum,
  # but also partitioned along the chunk_split axis
  # [b.YZ, t, h.YZ, q]

  local_accum_shape = jax.eval_shape(indexed_computation, 0, 0, rhs).shape
  # print(lhs.shape, rhs.shape, local_accum_shape)
  # we will concatenate in the loops over heads, so that we are sharded
  # only over batch
  embed_idx = 2
  final_embed_dim = local_accum_shape[embed_idx] * x_size
  final_accum_shape = list(local_accum_shape)
  final_accum_shape[embed_idx] = final_embed_dim
  final_accum = jnp.zeros(final_accum_shape, dtype=jnp.bfloat16)

  def indexed_update(x_i, update, final_accum):
    """Stacks alongs the dimension sharded by xy."""
    # print('f', final_accum.shape, 'u', update.shape, final_embed_dim)
    indices = [0] * len(final_accum.shape)
    indices[embed_idx] = x_i
    return jax.lax.dynamic_update_slice(final_accum, update, indices)

  # overlap chunk computation with x
  def z_loop_and_matmul(z_i, carrys):
    with jax.named_scope('z'):

      accum, rhs, y_i = carrys

      p = indexed_computation(y_i, z_i, rhs)  # index w/ x_i as we rebuild embed
      accum += p

      next_rhs = lax.ppermute(
          rhs, 'z', perm=[(j, (j + 1) % z_size) for j in range(z_size)])

      return accum, next_rhs, y_i

  # overlap z movement with entire x computation
  def y_loop(y_i, carrys):
    with jax.named_scope('y'):
      local_accum, rhs = carrys

      local_accum, rhs, _ = jax.lax.fori_loop(0, z_size - 1, z_loop_and_matmul,
                                              (local_accum, rhs, y_i))
      local_accum += indexed_computation(y_i, z_size - 1, rhs)

      # ideally don't want to do this y extra times
      next_rhs = lax.ppermute(
          rhs, 'y', perm=[(j, (j + 1) % y_size) for j in range(y_size)])

      return local_accum, next_rhs

  def collect_yz(final_accum, rhs, x_i):
    local_accum = jnp.zeros(local_accum_shape, dtype=jnp.bfloat16)
    local_accum, rhs = jax.lax.fori_loop(0, y_size - 1, y_loop,
                                         (local_accum, rhs))
    local_accum, rhs, _ = jax.lax.fori_loop(0, z_size - 1, z_loop_and_matmul,
                                            (local_accum, rhs, y_size - 1))
    # do one less loop to skip an uneeded permute
    local_accum += indexed_computation(y_size - 1, z_size - 1, rhs)
    # concatenate and slowly unshard by heads
    final_accum = indexed_update(x_i, local_accum, final_accum)
    return final_accum, rhs

  # overlap y movement with all z computation
  def x_loop(x_i, carrys):
    with jax.named_scope('x'):
      final_accum, rhs = carrys

      # we do the z loop one less time than we need to, as the weights
      # shift Z-1 times, then we do an x loop with the final set of z weights
      # [b.YZ, t, h.YZ, q]
      final_accum, rhs = collect_yz(final_accum, rhs, x_i)

      # not so costly to do this one extra time
      next_rhs = lax.ppermute(
          rhs, 'x', perm=[(j, (j + 1) % x_size) for j in range(x_size)])

      return (final_accum, next_rhs)

  final_accum, rhs = jax.lax.fori_loop(0, x_size - 1, x_loop,
                                       (final_accum, rhs))
  # we do one less y loop than we need, and instead finish off with the z loop
  # that would have been (to avoid a final permute)
  final_accum, rhs = collect_yz(final_accum, rhs, x_size - 1)

  # do we need a final x? No?

  return final_accum
