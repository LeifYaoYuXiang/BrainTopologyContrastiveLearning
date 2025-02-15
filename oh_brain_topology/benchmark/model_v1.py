import torch
import torch as th
import torch.nn as nn
from torch.nn import init
import torch.nn.functional as F
import dgl.function as fn
from dgl import DGLError
from dgl.utils import expand_as_pair


# reference: https://github.com/EdisonLeeeee/MedianGCN
class MedianConv(nn.Module):
    def __init__(self, in_feats, out_feats, norm='none', weight=True, bias=True, activation=None):
        super(MedianConv, self).__init__()
        if norm not in ('none', 'both', 'right', 'left'):
            raise DGLError('Invalid norm value. Must be either "none", "both", "right" or "left".'
                           ' But got "{}".'.format(norm))
        self._in_feats = in_feats
        self._out_feats = out_feats
        self._norm = norm

        if weight:
            self.weight = nn.Parameter(th.Tensor(in_feats, out_feats))
        else:
            self.register_parameter('weight', None)

        if bias:
            self.bias = nn.Parameter(th.Tensor(out_feats))
        else:
            self.register_parameter('bias', None)

        self.reset_parameters()

        self._activation = activation

    def reset_parameters(self):
        if self.weight is not None:
            init.xavier_uniform_(self.weight)
        if self.bias is not None:
            init.zeros_(self.bias)

    def forward(self, graph, feat, weight=None, edge_weight=None):
        with graph.local_scope():
            aggregate_fn = fn.copy_src('h', 'm')
            if edge_weight is not None:
                assert edge_weight.shape[0] == graph.number_of_edges()
                graph.edata['_edge_weight'] = edge_weight
                aggregate_fn = fn.u_mul_e('h', '_edge_weight', 'm')

            # (BarclayII) For RGCN on heterogeneous graphs we need to support GCN on bipartite.
            feat_src, feat_dst = expand_as_pair(feat, graph)
            if self._norm in ['left', 'both']:
                degs = graph.out_degrees().float().clamp(min=1)
                if self._norm == 'both':
                    norm = th.pow(degs, -0.5)
                else:
                    norm = 1.0 / degs
                shp = norm.shape + (1,) * (feat_src.dim() - 1)
                norm = th.reshape(norm, shp)
                feat_src = feat_src * norm

            if weight is not None:
                if self.weight is not None:
                    raise DGLError('External weight is provided while at the same time the'
                                   ' module has defined its own weight parameter. Please'
                                   ' create the module with flag weight=False.')
            else:
                weight = self.weight

            if self._in_feats > self._out_feats:
                # mult W first to reduce the feature size for aggregation.
                if weight is not None:
                    feat_src = th.matmul(feat_src, weight)
                graph.srcdata['h'] = feat_src
                graph.update_all(aggregate_fn, median_reduce)
                rst = graph.dstdata['h']
            else:
                # aggregate first then mult W
                graph.srcdata['h'] = feat_src
                graph.update_all(aggregate_fn, median_reduce)
                rst = graph.dstdata['h']
                if weight is not None:
                    rst = th.matmul(rst, weight)

            if self._norm in ['right', 'both']:
                degs = graph.in_degrees().float().clamp(min=1)
                if self._norm == 'both':
                    norm = th.pow(degs, -0.5)
                else:
                    norm = 1.0 / degs
                shp = norm.shape + (1,) * (feat_dst.dim() - 1)
                norm = th.reshape(norm, shp)
                rst = rst * norm

            if self.bias is not None:
                rst = rst + self.bias

            if self._activation is not None:
                rst = self._activation(rst)

            return rst

    def extra_repr(self):

        summary = 'in={_in_feats}, out={_out_feats}'
        summary += ', normalization={_norm}'
        if '_activation' in self.__dict__:
            summary += ', activation={_activation}'
        return summary.format(**self.__dict__)


def median_reduce(nodes):
    return {'h': th.median(nodes.mailbox['m'], dim=1).values}


class MedianModel(nn.Module):
    def __init__(self, model_config):
        super(MedianModel, self).__init__()
        in_feats = model_config['in_feats']
        n_hidden = model_config['n_hidden']
        n_classes = model_config['n_classes']
        n_layers = model_config['n_layers']
        node_each_graph = model_config['node_each_graph']
        activation = F.relu
        dropout = model_config['dropout']

        self.layers = nn.ModuleList()
        # input layer
        self.layers.append(MedianConv(in_feats, n_hidden, activation=activation))
        # hidden layers
        for i in range(n_layers - 1):
            self.layers.append(MedianConv(n_hidden, n_hidden, activation=activation))
        # 投影层
        self.classification = nn.Linear(node_each_graph * n_hidden, n_classes)
        self.node_each_graph = node_each_graph
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, graph):
        h = graph.ndata['feat'].to(torch.float32)
        for i, layer in enumerate(self.layers):
            if i != 0:
                h = self.dropout(h)
            h = layer(graph, h)
        h = h.reshape(1, -1)
        logits = self.classification(h)
        return logits

    def get_embedding(self, graph):
        h = graph.ndata['feat'].to(torch.float32)
        for i, layer in enumerate(self.layers):
            if i != 0:
                h = self.dropout(h)
            h = layer(graph, h)
        h = h.reshape(1, -1)
        return h
