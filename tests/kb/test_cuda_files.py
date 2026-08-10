"""CUDA files are indexed through the C++ grammar (G3).

`.cu`/`.cuh` were absent from the extension table, so a CUDA file produced zero
nodes. Measured on a large legacy C++ tree: 2 files, 8,793 lines, 0 nodes -- and a
comparator that does read them gave a better answer to "who calls this" than we did,
which is the concrete cost of the gap.
"""

import pathlib

from contextlake.kb.parse import (
    LANG_BY_EXT,
    index_repo_dir,
    is_indexable_name,
    parse_source,
)

_KERNEL = b"""#include <cuda_runtime.h>
__global__ void addKernel(float* out, const float* a, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = a[i] + 1.0f;
}
__device__ float helper(float x) { return x * 2.0f; }
class GpuBuffer {
public:
    void upload(const float* src, int n);
private:
    int n_;
};
void GpuBuffer::upload(const float* src, int n) {
    helper(1.0f);
}
"""


def test_cuda_extensions_are_registered():
    assert LANG_BY_EXT[".cu"] == "cpp"
    assert LANG_BY_EXT[".cuh"] == "cpp"
    assert is_indexable_name("k.cu", "gpu/k.cu")
    assert is_indexable_name("k.cuh", "gpu/k.cuh")


def test_a_kernel_file_yields_symbols():
    nodes, *_ = parse_source("r", "k.cu", _KERNEL, "cpp")
    names = {n.name for n in nodes}
    assert {"addKernel", "helper", "GpuBuffer", "upload"} <= names


def test_a_kernel_is_a_definition_not_a_call_target_only():
    nodes, *_ = parse_source("r", "k.cu", _KERNEL, "cpp")
    kernel = next(n for n in nodes if n.name == "addKernel")
    assert kernel.kind == "function"
    assert kernel.line_start > 0


def test_an_ordinary_call_inside_a_cuda_file_is_captured():
    _, _, calls, _ = parse_source("r", "k.cu", _KERNEL, "cpp")
    assert any(c[1] == "helper" for c in calls)


def test_launch_syntax_does_not_abort_the_file(tmp_path):
    """`kernel<<<grid, block>>>(...)` is not C++ and lands in a local ERROR region.
    tree-sitter degrades locally, so the REST of the file must still extract -- that
    is the property this test pins, not the launch itself."""
    src = _KERNEL.replace(b"    helper(1.0f);",
                          b"    addKernel<<<16, 256>>>(nullptr, src, n);\n    helper(1.0f);")
    nodes, _, calls, _ = parse_source("r", "k.cu", src, "cpp")
    assert {"addKernel", "helper", "GpuBuffer"} <= {n.name for n in nodes}
    assert any(c[1] == "helper" for c in calls)   # the plain call still resolves


def test_a_cuda_file_is_picked_up_by_the_indexer(tmp_path: pathlib.Path):
    """Through index_repo_dir, because the extension table only matters if the walker
    hands the file over."""
    (tmp_path / "k.cu").write_bytes(_KERNEL)
    shard = index_repo_dir(str(tmp_path), "r")
    assert any(n.file == "k.cu" for n in shard.nodes)
    assert any(n.name == "addKernel" for n in shard.nodes)
