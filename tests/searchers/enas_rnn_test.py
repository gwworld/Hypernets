# -*- coding:utf-8 -*-
__author__ = 'yangjian'
"""

"""
import tensorflow as tf
from tensorflow.keras.optimizers import Adam

from hypernets.core.search_space import HyperSpace
from hypernets.frameworks.keras.enas_common_ops import conv_layer
from hypernets.frameworks.keras.layers import Input
from hypernets.searchers.enas_rl_searcher import RnnController, EnasSearcher
from tensorflow.python.ops import clip_ops

baseline_decay = 0.999


class Test_EnasRnnController():
    def get_space(self):
        hp_dict = {}
        space = HyperSpace()
        with space.as_default():
            filters = 64
            in1 = Input(shape=(28, 28, 1,))
            conv_layer(hp_dict, 'normal', 0, [in1, in1], filters, 5)
            space.set_inputs(in1)
            return space

    def test_sample(self):
        rc = RnnController(search_space_fn=self.get_space)
        rc.reset()
        out1 = rc.sample()
        out2 = rc.sample()
        assert out1
        assert out2

    def test_searcher(self):
        enas_searcher = EnasSearcher(space_fn=self.get_space)
        sample = enas_searcher.sample()
        loss = enas_searcher.update_result(sample, 0.9)
        assert loss

