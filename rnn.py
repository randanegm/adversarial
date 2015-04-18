
from collections import OrderedDict
import functools

import numpy as np
from pylearn2.models.mlp import MLP, Layer, Softmax
from pylearn2.sandbox.nlp.linear.matrixmul import MatrixMul
from pylearn2.space import CompositeSpace, VectorSpace
from pylearn2.utils import sharedX
from pylearn2.utils.rng import make_theano_rng
import scipy
import theano
import theano.tensor as T


class GenerativeRecurrentMLP(MLP):
    def __init__(self, input_space, hidden_mlp, output_mlp, max_steps, irange,
                 num_classes, emb_irange=0.1,
                 *args, **kwargs):
        self.max_steps = max_steps
        self._scan_updates = OrderedDict()

        # Create new EOS class
        self.eos_id = num_classes
        num_classes += 1

        self.irange = irange
        self.num_classes = num_classes
        self.emb_dim = input_space.dim
        self.emb_irange = emb_irange

        self.hidden_mlp = hidden_mlp
        self.output_mlp = output_mlp

        self.softmax = Softmax(layer_name='softmax',
                               irange=irange,
                               binary_target_dim=1,
                               n_classes=num_classes,)

        layers = [self.hidden_mlp, self.output_mlp, self.softmax]
        super(GenerativeRecurrentMLP, self).__init__(layers, input_space=input_space,
                                                     *args, **kwargs)

        self.hidden_dim = hidden_mlp.layers[-1].dim
        self.set_input_space(input_space)
        hidden_mlp.set_input_space(CompositeSpace(components=[VectorSpace(dim=self.emb_dim),
                                                              VectorSpace(dim=self.hidden_dim)]))

    @functools.wraps(Layer.set_input_space)
    def set_input_space(self, space):
        self.input_space = space
        self.output_space = VectorSpace(dim=self.emb_dim)

        # Initialize embeddings
        E = self.rng.uniform(-self.emb_irange, self.emb_irange, (self.num_classes, self.emb_dim))
        self.E = sharedX(E, name='embeddings')

        # Initialize input -> hidden0 mapping
        W = self.rng.uniform(-self.irange, self.irange, (self.input_space.dim, self.hidden_dim))

        self._params = [
            self.E,
            sharedX(W, name='inp2hidden0'),
        ]

    @functools.wraps(Layer.get_params)
    def get_params(self):
        return self._params + super(GenerativeRecurrentMLP, self).get_params()

    @functools.wraps(Layer._modify_updates)
    def _modify_updates(self, updates):
        updates.update(self._scan_updates)

    @functools.wraps(Layer.fprop)
    def fprop(self, state_below):
        self.input_space.validate(state_below)

        _, W = self._params

        # First map input -> hidden
        # TODO bias?
        state_start = T.dot(state_below, W)

        # Now scan!
        finished = T.ones((state_below.shape[0],), dtype='int8')
        softmax_data = T.zeros((state_below.shape[0], self.num_classes), dtype=theano.config.floatX)
        z, updates = theano.scan(fn=self.fprop_step,
                                 outputs_info=(state_below, state_start, softmax_data, finished),
                                 n_steps=self.max_steps)

        # Pass on updates from scan operation
        self._scan_updates.update(updates)

        # Build return value
        ret_outputs, ret_hiddens, ret_softmaxes, _ = z
        return ret_outputs, ret_hiddens, ret_softmaxes

    def fprop_step(self, state_below, state_before, softmax_data_acc, finished_acc):
        new_hidden = self.hidden_mlp.fprop((state_below, state_before))
        output = self.output_mlp.fprop(new_hidden)

        # Calculate next input values
        softmax_data = self.softmax.fprop(output)
        best_idxs = T.argmax(output, axis=0)
        best_idxs = T.switch(T.eq(finished_acc, 1), self.eos_id, best_idxs)

        # Fetch embeddings
        new_input = self.E[best_idxs]

        # Check for EOS emissions and update our knowledge of which batch is finished
        finished_acc = T.eq(best_idxs, self.eos_id)
        all_finished = T.neq(finished_acc.min(), 0)

        return (new_input, new_hidden, softmax_data, finished_acc)#, theano.scan_module.until(all_finished)
