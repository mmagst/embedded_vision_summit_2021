from copy import deepcopy
from pathlib import Path
from time import time
from typing import Optional

import onnxruntime as rt
import torch
import torch.nn as nn
import torch.onnx as onnx
from onnxruntime.quantization import CalibrationDataReader
from onnxruntime.quantization import quantize_static
from torch.backends._nnapi.prepare import convert_model_to_nnapi
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
from torch.utils.mobile_optimizer import optimize_for_mobile
from torchvision.models.mobilenetv2 import _make_divisible


class ToyDataset(Dataset):

    def __init__(self):
        super(ToyDataset, self).__init__()
        self.len = 10

    def __len__(self) -> int:
        return self.len

    def __getitem__(self, item: int) -> torch.Tensor:
        return torch.rand((3, 224, 224))


class ConvBNReLU(nn.Module):

    def __init__(self, in_channels: int, out_channels: int, stride: int = 1):
        super(ConvBNReLU, self).__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=(3, 3),
                      stride=(stride, stride), padding=(1, 1), bias=False),
            nn.BatchNorm2d(num_features=out_channels),
            nn.ReLU()
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.layers(x)

    def fuse(self):
        torch.quantization.fuse_modules(self, ['layers.0', 'layers.1', 'layers.2'], inplace=True)


class OptimizedConvBNReLU(nn.Module):

    def __init__(self, in_channels: int, out_channels: int, stride: int = 1):
        super(OptimizedConvBNReLU, self).__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(in_channels=in_channels, out_channels=in_channels, kernel_size=(3, 3),
                      stride=(stride, stride), padding=(1, 1), bias=False, groups=in_channels),
            nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=(1, 1),
                      bias=False),
            nn.BatchNorm2d(num_features=out_channels),
            nn.ReLU()
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.layers(x)

    def fuse(self):
        torch.quantization.fuse_modules(self, ['layers.1', 'layers.2', 'layers.3'], inplace=True)


class ToyClassifier(nn.Module):

    def __init__(self, optimized: bool = False):
        super(ToyClassifier, self).__init__()

        block_params = [
            (3, 18, 1),
            (18, 36, 2),
            (36, 74, 1),
            (74, 146, 2),
            (146, 290, 1),
            (290, 578, 2),
            (578, 1154, 1),
            (1154, 1154, 2)
        ]
        if optimized:
            blocks = [OptimizedConvBNReLU(in_channels=3,
                                          out_channels=_make_divisible(block_params[0][1], 8),
                                          stride=block_params[0][2])]
            for in_channels, out_channels, stride in block_params[1:]:
                blocks.append(OptimizedConvBNReLU(in_channels=_make_divisible(in_channels, 8),
                                                  out_channels=_make_divisible(out_channels, 8),
                                                  stride=stride)
                              )
            in_features = _make_divisible(1154, 8)
        else:
            blocks = [ConvBNReLU(in_channels=in_channels,
                                 out_channels=out_channels,
                                 stride=stride)
                      for (in_channels, out_channels, stride) in block_params]
            in_features = 1154

        self.blocks = nn.ModuleList(blocks)
        self.pooling = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Conv2d(in_channels=in_features, out_channels=1000,
                                    kernel_size=(1, 1))

        self.quant = torch.quantization.QuantStub()
        self.dequant = torch.quantization.DeQuantStub()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x = x.contiguous(memory_format=torch.channels_last)
        x = self.quant(x)
        for block in self.blocks:
            x = block(x)
        features = self.pooling(x)
        logits = self.classifier(features)
        logits = self.dequant(logits)
        return logits

    def fuse(self):
        for block in self.blocks:
            block.fuse()


def script_and_serialize(model: nn.Module, path: str, opt_backend: Optional[str] = None):
    scripted_model = torch.jit.script(model)
    if opt_backend:
        scripted_model = optimize_for_mobile(script_module=scripted_model, backend=opt_backend)
    torch.jit.save(scripted_model, path)


def trace_and_serialize(model: nn.Module, example: torch.Tensor, path: str,
                        opt_backend: Optional[str] = None):
    with torch.no_grad():
        traced_model = torch.jit.trace(model, example_inputs=example)
    if opt_backend:
        traced_model = optimize_for_mobile(script_module=traced_model, backend=opt_backend)
    torch.jit.save(traced_model, path)


def training_loop():
    pass


def deploy_float(model: nn.Module, name: str):
    scripted_path = f'./{name}_float_scripted.pt'
    traced_path = f'./{name}_float_traced.pt'
    vulkan_path = f'./{name}_float_vulkan_traced.pt'
    model.eval()
    script_and_serialize(model, path=scripted_path)
    trace_and_serialize(model, example=torch.rand(1, 3, 224, 224), path=traced_path)
    script_and_serialize(model, path=vulkan_path, opt_backend='VULKAN')

    avg_time = benchmark_model(torch.jit.load(scripted_path))
    size = Path(scripted_path).stat().st_size / 1e6
    print(
        f'Benchmarking {scripted_path}: Avg. inference@CPU: {avg_time:3.2f} ms, Size: {size:2.2f} MB')
    avg_time = benchmark_model(torch.jit.load(traced_path))
    size = Path(traced_path).stat().st_size / 1e6
    print(
        f'Benchmarking {traced_path}: Avg. inference@CPU: {avg_time:3.2f} ms, Size: {size:2.2f} MB')
    avg_time = benchmark_model(torch.jit.load(vulkan_path))
    size = Path(vulkan_path).stat().st_size / 1e6
    print(
        f'Benchmarking {vulkan_path}: Avg. inference@CPU: {avg_time:3.2f} ms, Size: {size:2.2f} MB')


def deploy_quantized(dataloader: DataLoader, model: nn.Module, fuse: bool, name: str,
                     backend: str = 'qnnpack'):
    model = deepcopy(model)
    torch.backends.quantized.engine = backend
    model.qconfig = torch.quantization.get_default_qconfig(backend)
    path = f'./{name}_quant'

    model = model.eval()
    if fuse:
        model.fuse()
        path += '_fused'

    model_prepared = torch.quantization.prepare(model)
    for sample in dataloader:
        model_prepared(sample)
    model_quantized = torch.quantization.convert(model_prepared)

    scripted_path = path + '_scripted.pt'
    traced_path = path + '_traced.pt'
    script_and_serialize(model_quantized, path=scripted_path, opt_backend='CPU')
    trace_and_serialize(model_quantized, example=torch.rand(1, 3, 224, 224), path=traced_path,
                        opt_backend='CPU')

    avg_time = benchmark_model(torch.jit.load(scripted_path))
    size = Path(scripted_path).stat().st_size / 1e6
    print(
        f'Benchmarking {scripted_path}: Avg. inference@CPU: {avg_time:3.2f} ms, Size: {size:2.2f} MB')
    avg_time = benchmark_model(torch.jit.load(traced_path))
    size = Path(traced_path).stat().st_size / 1e6
    print(
        f'Benchmarking {traced_path}: Avg. inference@CPU: {avg_time:3.2f} ms, Size: {size:2.2f} MB')


def deploy_nnapi(dataloader: DataLoader, model: nn.Module, fuse: bool, name: str,
                 backend: str = 'qnnpack'):
    model = deepcopy(model)
    torch.backends.quantized.engine = backend
    model.qconfig = torch.quantization.get_default_qconfig(backend)
    path = f'./{name}_nnapi'

    model = model.eval()
    if fuse:
        model.fuse()
        path += '_fused'

    model_prepared = torch.quantization.prepare(model)
    for sample in dataloader:
        model_prepared(sample)
    model_quantized = torch.quantization.convert(model_prepared)

    input_float = torch.rand(1, 3, 224, 224)

    quantizer = model_quantized.quant
    dequantizer = model_quantized.dequant
    model_quantized.quant = torch.nn.Identity()
    model_quantized.dequant = torch.nn.Identity()
    input_tensor = quantizer(input_float)

    input_tensor = input_tensor.contiguous(memory_format=torch.channels_last)
    input_tensor.nnapi_nhwc = True

    with torch.no_grad():
        model_quantized_traced = torch.jit.trace(model_quantized, input_tensor)
    nnapi_model = convert_model_to_nnapi(model_quantized_traced, input_tensor)
    nnapi_model_float_interface = torch.jit.script(
        torch.nn.Sequential(quantizer, nnapi_model, dequantizer))

    traced_path = path + '_traced.pt'
    traced_float_path = path + '_float_interface_traced.pt'
    nnapi_model.save(traced_path)
    nnapi_model_float_interface.save(traced_float_path)


class ONNXQuantizationDataReader(CalibrationDataReader):
    def __init__(self,
                 quant_loader: DataLoader,
                 input_name: str):
        self.data = []
        for inputs in quant_loader:
            # Here we unroll batch size as dynamic axis is not supported and
            # batch size is then hardcoded to 1
            for input_frame in inputs:
                self.data.append(input_frame.unsqueeze(0).numpy())

        self.iter = iter([{input_name: d} for d in self.data])

    def get_next(self):
        return next(self.iter, None)


def deploy_onnx_quantized(dataloader: DataLoader, model: nn.Module, fuse: bool, name: str):
    model = deepcopy(model)
    path = f'./{name}'

    model = model.eval()
    if fuse:
        model.fuse()
        path += '_fused'

    float_path = path + '_float.onnx'
    quantized_path = path + '_quant.onnx'
    example_input = torch.rand(1, 3, 224, 224)
    onnx.export(model=model, args=(example_input,), f=float_path, input_names=['input_image'],
                output_names=['logits'], opset_version=12)
    onnx_q_loader = ONNXQuantizationDataReader(quant_loader=dataloader, input_name='input_image')
    quantize_static(model_input=float_path, model_output=quantized_path,
                    calibration_data_reader=onnx_q_loader)

    avg_time = benchmark_onnx_model(rt.InferenceSession(float_path))
    size = Path(float_path).stat().st_size / 1e6
    print(
        f'Benchmarking {float_path}: Avg. inference@CPU: {avg_time:3.2f} ms, Size: {size:2.2f} MB')
    avg_time = benchmark_onnx_model(rt.InferenceSession(quantized_path))
    size = Path(quantized_path).stat().st_size / 1e6
    print(
        f'Benchmarking {quantized_path}: Avg. inference@CPU: {avg_time:3.2f} ms, Size: {size:2.2f} MB')


def benchmark_onnx_model(model: rt.InferenceSession, n_samples: int = 100) -> float:
    avg_time = 0
    for i in range(n_samples):
        tensor = torch.rand((1, 3, 224, 224)).numpy()
        start = time()
        model.run(None, {'input_image': tensor})
        elapsed = time() - start
        avg_time += elapsed
    avg_time /= n_samples
    return avg_time * 1000


def benchmark_model(model: nn.Module, n_samples: int = 100) -> float:
    avg_time = 0
    for i in range(n_samples):
        tensor = torch.rand((1, 3, 224, 224))
        start = time()
        model(tensor)
        elapsed = time() - start
        avg_time += elapsed
    avg_time /= n_samples
    return avg_time * 1000


def main():
    for optimized, name in [(False, 'classifier'), (True, 'optimized_classifier')]:
        model = ToyClassifier(optimized=optimized)
        n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print(f'Model {name} has {n_params / 1e6:2.2f} M parameters')
        dataset = ToyDataset()
        dataloader = DataLoader(dataset)

        training_loop()

        deploy_float(model, name=name)
        deploy_onnx_quantized(dataloader, model, fuse=False, name=name)
        deploy_onnx_quantized(dataloader, model, fuse=True, name=name)
        deploy_quantized(dataloader, model, fuse=False, name=name, backend='fbgemm')
        deploy_quantized(dataloader, model, fuse=True, name=name, backend='fbgemm')
        deploy_nnapi(dataloader, model, fuse=True, name=name)


if __name__ == '__main__':
    main()
