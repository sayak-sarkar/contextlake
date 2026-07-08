"""Tests for Terraform/HCL extraction (kb/hcl.py)."""

from datetime import date

from contextlake.kb.hcl import parse_hcl
from contextlake.kb.parse import index_repo_dir

MAIN_TF = b"""
variable "region" {
  type    = string
  default = "us-east-1"
}

variable "bucket_name" {
  type = string
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "logs" {
  bucket = var.bucket_name
  region = var.region

  versioning {
    enabled = true
  }
}

resource "aws_s3_bucket_policy" "logs_policy" {
  bucket     = aws_s3_bucket.logs.id
  account    = data.aws_caller_identity.current.account_id
  depends_on = [aws_s3_bucket.logs]
}

module "network" {
  source = "./modules/network"
  region = var.region
}

locals {
  full_name = "${var.bucket_name}-logs"
}

output "bucket_arn" {
  value = aws_s3_bucket.logs.arn
}
"""


def _by_kind(nodes):
    out: dict[str, set] = {}
    for n in nodes:
        out.setdefault(n.kind, set()).add(n.name)
    return out


def test_parse_hcl_extracts_top_level_block_defs():
    nodes, _refs = parse_hcl("infra/net", "main.tf", MAIN_TF,
                             verified_at=date(2026, 7, 8))
    kinds = _by_kind(nodes)
    assert kinds["variable"] == {"var.region", "var.bucket_name"}
    assert kinds["data"] == {"data.aws_caller_identity.current"}
    assert kinds["resource"] == {"aws_s3_bucket.logs", "aws_s3_bucket_policy.logs_policy"}
    assert kinds["module"] == {"module.network"}
    assert kinds["local"] == {"local.full_name"}
    assert kinds["output"] == {"output.bucket_arn"}
    # nested blocks are NOT nodes
    all_names = {n.name for n in nodes}
    assert "versioning" not in all_names
    assert not any(n.kind == "versioning" for n in nodes)
    # every node carries file + lang + provenance-ready line
    res = next(n for n in nodes if n.name == "aws_s3_bucket.logs")
    assert res.file == "main.tf" and res.lang == "hcl"
    assert res.qualified_name == "main.tf::aws_s3_bucket.logs"
    assert res.line_start and res.line_end and res.line_end >= res.line_start


def _addr_of(nodes, node_id):
    return next(n.name for n in nodes if n.id == node_id)


def test_parse_hcl_reconstructs_references():
    nodes, refs = parse_hcl("infra/net", "main.tf", MAIN_TF,
                            verified_at=date(2026, 7, 8))
    # translate (src_id, address) -> (src_address, target_address)
    pairs = {(_addr_of(nodes, src), tgt) for src, tgt, _f, _ln in refs}

    assert ("aws_s3_bucket.logs", "var.bucket_name") in pairs
    assert ("aws_s3_bucket.logs", "var.region") in pairs
    # implicit (.id) AND explicit (depends_on=[...]) both reference the bucket
    assert ("aws_s3_bucket_policy.logs_policy", "aws_s3_bucket.logs") in pairs
    assert ("aws_s3_bucket_policy.logs_policy", "data.aws_caller_identity.current") in pairs
    assert ("module.network", "var.region") in pairs
    assert ("local.full_name", "var.bucket_name") in pairs
    assert ("output.bucket_arn", "aws_s3_bucket.logs") in pairs
    # meta roots (each/count/path/self/terraform) never produce a ref address
    assert not any(t.startswith(("each.", "count.", "path.", "self.")) for _s, t, _f, _l in refs)


def test_index_repo_dir_resolves_hcl_depends_on(tmp_path):
    # two files in the same module dir: vars split from resources (cross-file)
    (tmp_path / "variables.tf").write_text(
        'variable "region" {\n  type = string\n}\n'
        'variable "bucket_name" {\n  type = string\n}\n'
    )
    (tmp_path / "main.tf").write_text(
        'resource "aws_s3_bucket" "logs" {\n'
        '  bucket = var.bucket_name\n'
        '  region = var.region\n'
        '}\n\n'
        'resource "aws_s3_bucket_policy" "p" {\n'
        '  bucket     = aws_s3_bucket.logs.id\n'
        '  depends_on = [aws_s3_bucket.logs]\n'
        '}\n'
    )
    shard = index_repo_dir(str(tmp_path), "infra/net")
    name = {n.id: n.name for n in shard.nodes}
    dep = {(name[e.src], name[e.dst]) for e in shard.edges if e.relation == "depends_on"}

    # cross-file var reference resolves
    assert ("aws_s3_bucket.logs", "var.bucket_name") in dep
    assert ("aws_s3_bucket.logs", "var.region") in dep
    # resource-to-resource dependency resolves (implicit + explicit, deduped)
    assert ("aws_s3_bucket_policy.p", "aws_s3_bucket.logs") in dep
    # HCL nodes exist with the right kinds
    kinds = {n.kind for n in shard.nodes}
    assert {"resource", "variable"} <= kinds


def test_index_repo_dir_languages_filter_excludes_hcl(tmp_path):
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    (tmp_path / "app.py").write_text("def f():\n    pass\n")
    shard = index_repo_dir(str(tmp_path), "r", languages=["python"])
    kinds = {n.kind for n in shard.nodes}
    assert "resource" not in kinds  # hcl gated out
    assert "function" in kinds
