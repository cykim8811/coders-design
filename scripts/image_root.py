"""Extract pinned Docker Hub image layers into a disposable root (no Docker cache)."""

import argparse
import json
import shutil
import subprocess
import urllib.request


def fetch(url, token=None):
    headers = {
        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args()
    name, tag = args.image.rsplit(":", 1)
    token = json.load(
        fetch(
            "https://auth.docker.io/token?service=registry.docker.io&scope=repository:"
            + name
            + ":pull"
        )
    )["token"]
    base = "https://registry-1.docker.io/v2/" + name
    with fetch(base + "/manifests/" + tag, token) as response:
        digest = response.headers.get("Docker-Content-Digest")
        manifest = json.load(response)
    if "manifests" in manifest:
        digest = next(
            m["digest"]
            for m in manifest["manifests"]
            if m["platform"].get("architecture") == "amd64" and m["platform"].get("os") == "linux"
        )
        manifest = json.load(fetch(base + "/manifests/" + digest, token))
    print(
        json.dumps(
            {
                "image": args.image,
                "digest": digest,
                "compressed_bytes": sum(x["size"] for x in manifest["layers"]),
            }
        ),
        flush=True,
    )
    if not args.destination:
        return
    from pathlib import Path

    root = Path(args.destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for layer in manifest["layers"]:
        # Official release images are the trust boundary; extraction is confined to this root.
        with fetch(base + "/blobs/" + layer["digest"], token) as response:
            proc = subprocess.Popen(
                [
                    "tar",
                    "-x",
                    "--zstd" if "zstd" in layer["mediaType"] else "-z",
                    "--no-same-owner",
                    "--exclude=dev",
                    "--exclude=proc",
                    "--exclude=sys",
                    "-C",
                    str(root),
                ],
                stdin=subprocess.PIPE,
            )
            try:
                while chunk := response.read(1024 * 1024):
                    proc.stdin.write(chunk)
                proc.stdin.close()
            finally:
                if proc.wait():
                    raise RuntimeError("layer extraction failed")
    config = json.load(fetch(base + "/blobs/" + manifest["config"]["digest"], token))
    (root / "image-config.json").write_text(json.dumps(config, indent=2))
    for name in ("hosts", "resolv.conf"):
        shutil.copyfile("/etc/" + name, root / "etc" / name)


if __name__ == "__main__":
    main()
