"""Capture and seal a bounded, research-only Context T0 evidence bundle.

This utility is deliberately file-to-file. It does not contact Kubernetes,
Redis, PostgreSQL, a broker, or a production API. Operators export bounded
artifacts first, provide one database/server T0 timestamp, and seal them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

SCHEMA_VERSION = "context-t0-capture-v1"
PRECAPTURE_SCHEMA_VERSION = "context-t0-precapture-v1"
V1_CONFIG_HASH = "1f1da2a63d69ac79e4aca21d0de33c860e76f4c33d9bd321cb50b20353114e1e"
V1_STRATEGY_FINGERPRINT = "6dda2523e15edbc0e2d123878367f21ffaec70219272aa409193c2fc45b7c9bc"
V2_CONTRACT_HASH = "4430542fb8d249d6338ead1fb745664a2e44836c4123e16f069b0c48bd69e107"
V2_PARAMETER_HASH = "ad897dff76e5b119d52fe7f05203d25d214b61c1dff69be18b9e8f784284335f"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _source_commit() -> str | None:
    try:
        return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _parse_timestamp(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    return None


def _artifact_stats(path: Path, kind: str) -> dict[str, object]:
    """Return bounded stats for JSONL/ledger artifacts without interpreting them."""
    stats: dict[str, object] = {"record_count": None, "first_timestamp": None, "last_timestamp": None}
    if kind not in {"jsonl", "ledger", "event-stream"} and path.suffix != ".jsonl":
        return stats
    timestamps: list[str] = []
    count = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                for key in ("event_timestamp", "event_time", "timestamp", "created_at", "occurred_at"):
                    timestamp = _parse_timestamp(row.get(key))
                    if timestamp is not None:
                        timestamps.append(timestamp)
                        break
    stats["record_count"] = count
    if timestamps:
        stats["first_timestamp"] = min(timestamps)
        stats["last_timestamp"] = max(timestamps)
    return stats


def _artifact_entry(name: str, source: Path, destination: Path, kind: str | None) -> dict[str, object]:
    resolved_kind = kind or ("jsonl" if source.suffix == ".jsonl" else "file")
    return {"filename": name, "type": resolved_kind, "sha256": _sha256(destination),
            "byte_size": destination.stat().st_size, **_artifact_stats(destination, resolved_kind),
            "source": str(source.resolve())}


def freeze_precapture(output_dir: Path, deployment: Path, *, captured_at_utc: str | None = None) -> dict:
    """Freeze deployment provenance while Kubernetes is healthy.

    The resulting directory is immutable input to ``capture``.  It contains no
    live Kubernetes client and can be consumed after the API becomes unavailable.
    """
    if output_dir.exists():
        raise FileExistsError(f"precapture destination already exists: {output_dir}")
    if not deployment.is_file():
        raise FileNotFoundError(deployment)
    output_dir.mkdir(parents=True)
    destination = output_dir / "deployment.json"
    shutil.copyfile(deployment, destination)
    manifest = {"schema": PRECAPTURE_SCHEMA_VERSION, "status": "FROZEN",
                "captured_at_utc": captured_at_utc or datetime.now(timezone.utc).isoformat(),
                "artifacts": [_artifact_entry("deployment.json", deployment, destination, "deployment")]}
    manifest_sha = hashlib.sha256(_canonical(manifest)).hexdigest()
    _write_json(output_dir / "manifest.json", manifest)
    (output_dir / "manifest.sha256").write_text(manifest_sha + "\n", encoding="utf-8")
    _write_json(output_dir / "seal.json", {"schema": PRECAPTURE_SCHEMA_VERSION, "status": "FROZEN",
                                             "manifest_sha256": manifest_sha,
                                             "frozen_at_utc": manifest["captured_at_utc"]})
    return {**manifest, "manifest_sha256": manifest_sha}


def verify_precapture(output_dir: Path) -> dict:
    manifest_path, seal_path, hash_path = (output_dir / name for name in ("manifest.json", "seal.json", "manifest.sha256"))
    if not manifest_path.is_file() or not seal_path.is_file() or not hash_path.is_file():
        raise ValueError("deployment precapture is not sealed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != PRECAPTURE_SCHEMA_VERSION or manifest.get("status") != "FROZEN":
        raise ValueError("invalid deployment precapture")
    expected = hashlib.sha256(_canonical(manifest)).hexdigest()
    if expected != hash_path.read_text(encoding="utf-8").strip():
        raise ValueError("deployment precapture manifest tampering detected")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if seal.get("manifest_sha256") != expected:
        raise ValueError("deployment precapture seal mismatch")
    artifacts = manifest.get("artifacts", [])
    if len(artifacts) != 1 or artifacts[0].get("filename") != "deployment.json":
        raise ValueError("deployment precapture must contain exactly deployment.json")
    if seal.get("schema") != PRECAPTURE_SCHEMA_VERSION or seal.get("status") != "FROZEN":
        raise ValueError("invalid deployment precapture seal")
    for artifact in artifacts:
        path = output_dir / str(artifact["filename"])
        if not path.is_file() or _sha256(path) != artifact["sha256"]:
            raise ValueError(f"deployment precapture artifact tampering detected: {path.name}")
    return {**manifest, "manifest_sha256": expected}


def _require(mapping: Mapping[str, object], key: str, prefix: str = "") -> object:
    value = mapping.get(key)
    if value is None or value == "":
        raise ValueError(f"missing required field: {prefix}{key}")
    return value


def validate_metadata(metadata: Mapping[str, object]) -> None:
    required = (
        "experiment_id", "captured_at_utc", "t0_utc", "capture_started_at", "capture_completed_at",
        "platform_source_commit", "gitops_revision", "runtime_image", "runtime_image_digest",
        "migration_version", "schema_fingerprint", "strategy_v1", "strategy_v2",
        "execution_safety", "evidence_boundaries", "source_skew", "ledger", "deployment_precapture",
    )
    for key in required:
        _require(metadata, key)
    v1, v2, safety = metadata["strategy_v1"], metadata["strategy_v2"], metadata["execution_safety"]
    if not isinstance(v1, Mapping) or not isinstance(v2, Mapping) or not isinstance(safety, Mapping):
        raise ValueError("strategy_v1, strategy_v2, and execution_safety must be objects")
    for key in ("strategy_id", "strategy_version", "config_hash", "strategy_fingerprint"):
        _require(v1, key, "strategy_v1.")
    if v1["config_hash"] != V1_CONFIG_HASH or v1["strategy_fingerprint"] != V1_STRATEGY_FINGERPRINT:
        raise ValueError("V1 identity does not match the pinned contract")
    for key in ("strategy_id", "strategy_version", "mode", "min_planned_r", "broker_writes",
                "contract_hash", "parameter_hash"):
        _require(v2, key, "strategy_v2.")
    pre = metadata["deployment_precapture"]
    if not isinstance(pre, Mapping):
        raise ValueError("deployment_precapture must be an object")
    for key in ("directory", "manifest_sha256", "captured_at_utc"):
        _require(pre, key, "deployment_precapture.")
    skew = metadata["source_skew"]
    if not isinstance(skew, Mapping):
        raise ValueError("source_skew must be an object")
    for source, details in skew.items():
        if not isinstance(details, Mapping):
            raise ValueError(f"source_skew.{source} must be an object")
        for key in ("source_timestamp", "capture_timestamp", "offset_from_t0_ms", "max_allowed_skew_ms"):
            _require(details, key, f"source_skew.{source}.")
        if abs(float(details["offset_from_t0_ms"])) > float(details["max_allowed_skew_ms"]):
            raise ValueError(f"source freshness exceeded for {source}")
    if v2["mode"] != "RESEARCH_ONLY" or float(v2["min_planned_r"]) != 1.0:
        raise ValueError("V2 must be RESEARCH_ONLY with min_planned_r=1.0")
    if v2["broker_writes"] is not False or v2["contract_hash"] != V2_CONTRACT_HASH or v2["parameter_hash"] != V2_PARAMETER_HASH:
        raise ValueError("V2 identity or broker-write setting does not match the pinned contract")
    allowlist = safety.get("execution_allowlist", [])
    if not isinstance(allowlist, list) or any("CONTEXT_STRUCTURE_RETRACE_V2" in str(x) for x in allowlist):
        raise ValueError("V2 must be absent from the execution allowlist")
    if safety.get("v2_execution_enabled") is not False:
        raise ValueError("V2 execution must be disabled")
    if int(safety.get("v2_execution_intent_count", -1)) != 0 or int(safety.get("v2_execution_attempt_count", -1)) != 0:
        raise ValueError("V2 execution intent and attempt counts must be zero")


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def capture(output_dir: Path, artifacts: Iterable[tuple[str, Path] | tuple[str, Path, str]], *,
            metadata: Mapping[str, object] | None = None, capture_id: str | None = None,
            source_commit: str | None = None, precapture_dir: Path | None = None) -> dict:
    """Seal a new T0 bundle from frozen metadata and authoritative source files.

    ``precapture_dir`` is mandatory by design: deployment provenance must have
    been frozen before T0, so this phase never needs Kubernetes access.
    """
    if output_dir.exists():
        raise FileExistsError(f"capture destination already exists: {output_dir}")
    requested = list(artifacts)
    if not requested:
        raise ValueError("at least one evidence artifact is required")
    if precapture_dir is None:
        raise ValueError("deployment precapture is required before T0 capture")
    precapture = verify_precapture(precapture_dir)
    metadata = dict(metadata or {})
    if source_commit and "platform_source_commit" not in metadata:
        metadata["platform_source_commit"] = source_commit
    validate_metadata(metadata)
    if metadata["deployment_precapture"]["manifest_sha256"] != precapture["manifest_sha256"]:
        raise ValueError("metadata does not reference the supplied deployment precapture")
    t0 = str(metadata["t0_utc"])
    try:
        precapture_time = datetime.fromisoformat(str(precapture["captured_at_utc"]).replace("Z", "+00:00"))
        t0_time = datetime.fromisoformat(t0.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("precapture and t0 timestamps must be ISO-8601") from exc
    if precapture_time > t0_time:
        raise ValueError("deployment precapture occurred after T0")
    frozen_deployment = precapture_dir / "deployment.json"
    if any(item[0] == "deployment.json" for item in requested):
        raise ValueError("deployment.json must come from the frozen precapture")
    for item in requested:
        name, source = item[:2]
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"artifact name must be a single filename: {name!r}")
        if not source.is_file():
            raise FileNotFoundError(source)
    output_dir.mkdir(parents=True)
    incomplete = {"schema": SCHEMA_VERSION, "status": "INCOMPLETE",
                  "experiment_id": metadata["experiment_id"], "capture_id": capture_id or output_dir.name}
    _write_json(output_dir / "capture-status.json", incomplete)
    try:
        shutil.copyfile(frozen_deployment, output_dir / "deployment.json")
        entries = [_artifact_entry("deployment.json", frozen_deployment, output_dir / "deployment.json", "deployment")]
        for item in requested:
            name, source = item[:2]
            kind = item[2] if len(item) == 3 else None
            destination = output_dir / name
            shutil.copyfile(source, destination)
            entries.append(_artifact_entry(name, source, destination, kind))
        manifest = {"schema": SCHEMA_VERSION, "status": "SEALED",
                    "experiment_id": metadata["experiment_id"], "capture_id": capture_id or output_dir.name,
                    **metadata, "source_commit": source_commit or _source_commit(), "artifacts": entries,
                    "production_writes": False}
        validate_metadata(manifest)
        manifest_sha = hashlib.sha256(_canonical(manifest)).hexdigest()
        _write_json(output_dir / "manifest.json", manifest)
        (output_dir / "manifest.sha256").write_text(manifest_sha + "\n", encoding="utf-8")
        _write_json(output_dir / "seal.json", {"schema": SCHEMA_VERSION, "status": "SEALED",
                                                 "experiment_id": manifest["experiment_id"],
                                                 "manifest_sha256": manifest_sha,
                                                 "sealed_at_utc": datetime.now(timezone.utc).isoformat()})
        (output_dir / "capture-status.json").unlink()
        return {**manifest, "manifest_sha256": manifest_sha}
    except Exception:
        incomplete["reason"] = "capture failed before seal"
        _write_json(output_dir / "capture-status.json", incomplete)
        raise


def verify_sealed_capture(output_dir: Path) -> dict:
    if (output_dir / "capture-status.json").exists():
        raise ValueError("capture is INCOMPLETE")
    manifest_path, seal_path, hash_path = (output_dir / name for name in ("manifest.json", "seal.json", "manifest.sha256"))
    if not manifest_path.is_file() or not seal_path.is_file() or not hash_path.is_file():
        raise ValueError("capture is not sealed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = hashlib.sha256(_canonical(manifest)).hexdigest()
    recorded = hash_path.read_text(encoding="utf-8").strip()
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if expected != recorded or expected != seal.get("manifest_sha256"):
        raise ValueError("manifest tampering detected")
    for artifact in manifest.get("artifacts", []):
        path = output_dir / str(artifact["filename"])
        if not path.is_file() or _sha256(path) != artifact["sha256"]:
            raise ValueError(f"artifact tampering detected: {path.name}")
    validate_metadata(manifest)
    return manifest


def records_at_t0(records: Iterable[Mapping[str, object]], t0_utc: str) -> list[Mapping[str, object]]:
    """Apply the inclusive boundary: timestamp < T0 is PRE_T0; >= T0 is forward."""
    return [record for record in records if str(record.get("timestamp", record.get("event_timestamp", record.get("event_time", "")))) >= t0_utc]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("precapture", help="freeze deployment provenance before T0")
    pre.add_argument("output", type=Path)
    pre.add_argument("--deployment", type=Path, required=True)
    pre.add_argument("--captured-at-utc")
    cap = commands.add_parser("capture", help="seal a T0 bundle without Kubernetes access")
    cap.add_argument("output", type=Path)
    cap.add_argument("--metadata", type=Path, required=True)
    cap.add_argument("--precapture", type=Path, required=True)
    cap.add_argument("--state", type=Path)
    cap.add_argument("--market-data", type=Path)
    cap.add_argument("--publication-ledger", type=Path)
    cap.add_argument("--execution-ledger", type=Path)
    cap.add_argument("--schema", type=Path)
    verify = commands.add_parser("verify", help="verify a sealed T0 bundle")
    verify.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "precapture":
        print(json.dumps(freeze_precapture(args.output, args.deployment, captured_at_utc=args.captured_at_utc), indent=2, sort_keys=True))
        return
    if args.command == "verify":
        print(json.dumps(verify_sealed_capture(args.output), indent=2, sort_keys=True))
        return
    candidates = (("state.json", args.state, "state"), ("market_data.jsonl", args.market_data, "jsonl"),
                  ("publication_ledger.jsonl", args.publication_ledger, "ledger"),
                  ("execution_ledger.jsonl", args.execution_ledger, "ledger"),
                  ("schema.json", args.schema, "schema"))
    artifacts = [(name, path, kind) for name, path, kind in candidates if path is not None]
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    print(json.dumps(capture(args.output, artifacts, metadata=metadata, precapture_dir=args.precapture), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
