from typing import Optional

import torch
import torch.nn as nn

from yadrl.networks.body_parameter import BodyParameters
from yadrl.networks.layer import Layer


class Body(nn.Module):
    def __init__(self, parameters: BodyParameters):
        super().__init__()
        self._body_parameters: BodyParameters = parameters
        self._body = self._build_network()

    def forward(self,
                x_primary: torch.Tensor,
                x_secondary: Optional[torch.Tensor] == None) -> torch.Tensor:
        for i, layer in enumerate(self._body):
            if i == self._body_parameters.action_layer:
                x_primary = torch.cat((x_primary, x_secondary), dim=1)
            x_primary = layer(x_primary)
        return x_primary

    def sample_noise(self):
        for layer in self._body:
            layer.sample_noise()

    def reset_noise(self):
        for layer in self._body:
            layer.reset_noise()

    def _build_network(self) -> nn.Module:
        body = nn.ModuleList()
        input_size = self._body_parameters.input.primary
        for i, params in enumerate(self._body_parameters.layers):
            if self._body_parameters.action_layer == i:
                input_size += self._body_parameters.input.secondary
            if params['layer_type'] == 'flatten':
                layer = nn.Flatten()
            else:
                layer = Layer.build(input_size, **params)
            input_size = params['out_dim']
            body.append(layer)
        return body

    @property
    def output_dim(self) -> int:
        if self._body_parameters.output_dim:
            return self._body_parameters.output_dim
        return self._body[-1].output_dim
