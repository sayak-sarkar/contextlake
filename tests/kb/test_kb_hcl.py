"""Tests for Terraform/HCL extraction (kb/hcl.py)."""

from datetime import date

from contextlake.kb.hcl import parse_hcl

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
