"""asset-archaeologist <command>

    scan      render + embed every pack in the library folders (resumable)
    serve     the browser page and search API on http://127.0.0.1:<port>/
    find      search from the command line; many queries in one call (for agents)
    similar   models that look like a given one
    doctor    check Blender, GPU / torch, settings and disk space
    config    show or change settings
    tag       re-score every model against the tag list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod


def _cfg(args):
    return config_mod.load(Path(args.config) if getattr(args, "config", None) else None)


def cmd_scan(args):
    cfg = _cfg(args)
    from . import scan
    if args.plan:
        packs, todo = scan.plan(cfg)
        print(f"{len(packs)} packs found, {len(packs) - len(todo)} done, {len(todo)} to do")
        for p in todo[: args.max or None]:
            print("  ", p)
        return 0
    if not args.paths and not cfg.library_dirs:
        print("No library folders are set. Add one with:\n  asset-archaeologist config --add-library <folder>\n"
              "or on the Settings screen of the page (asset-archaeologist serve --open).")
        return 2
    if args.device:
        cfg.device = args.device
    try:
        r = scan.run(cfg, procs=args.procs, max_packs=args.max, paths=args.paths or None, force=args.force,
                     limit=args.limit, only=args.only, keep=args.keep)
    except Exception as e:
        print(f"scan failed: {e}")
        return 1
    print(f"done: {r['models']} models from {r['packs']} packs, {r['failed_packs']} packs failed")
    return 0 if not r["failed_packs"] else 1


def cmd_serve(args):
    cfg = _cfg(args)
    from . import server
    server.serve(cfg, port=args.port, open_browser=args.open)
    return 0


def cmd_find(args):
    cfg = _cfg(args)
    from . import find
    find.find(cfg, args.queries, k=args.k, pack=args.pack, min_m=args.min_m, max_m=args.max_m, by=args.by,
              as_json=args.json, port=args.port)
    return 0


def cmd_similar(args):
    cfg = _cfg(args)
    from . import find
    find.similar(cfg, args.pack, args.model, k=args.k, as_json=args.json, port=args.port)
    return 0


def cmd_doctor(args):
    cfg = _cfg(args)
    from . import doctor
    return doctor.run(cfg, fetch_model=args.fetch_model)


def cmd_config(args):
    cfg = _cfg(args)
    changed = False
    for p in args.add_library or []:
        p = str(Path(p).expanduser().resolve())
        if p not in cfg.libraries:
            cfg.libraries.append(p)
            changed = True
    for p in args.remove_library or []:
        r = str(Path(p).expanduser().resolve())
        cfg.libraries = [x for x in cfg.libraries if x not in (p, r)]
        changed = True
    vals = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        vals[k.strip()] = v
    if vals:
        try:
            config_mod.update(cfg, vals)
        except ValueError as e:
            print(e)
            return 2
        changed = True
    if changed:
        config_mod.save(cfg)
    if args.json:
        print(json.dumps({**cfg.to_dict(), "config_file": str(cfg.path), "output_dir": str(cfg.output_dir),
                          "work_dir": str(cfg.work_dir), "blender_found": str(cfg.blender_path() or "")}, indent=1))
    else:
        print(f"settings file: {cfg.path}\n")
        print(cfg.path.read_text("utf-8"))
    return 0


def cmd_tag(args):
    cfg = _cfg(args)
    from . import embed
    n = embed.retag_all(cfg)
    print(f"re-tagged {n} packs")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="asset-archaeologist",
                                 description="Enable agents to find human art you already own.")
    ap.add_argument("--version", action="version", version=f"asset-archaeologist {__version__}")
    ap.add_argument("--config", help="settings file to use (default: see 'asset-archaeologist config')")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="render + embed every pack in the library folders (resumable)")
    s.add_argument("paths", nargs="*", type=Path, help="only these packs (zip, .unitypackage, .blend, folder)")
    s.add_argument("--plan", action="store_true", help="list what would be scanned and stop")
    s.add_argument("--procs", type=int, default=0, help="Blender processes at once (default: settings)")
    s.add_argument("--max", type=int, default=0, help="at most this many packs")
    s.add_argument("--force", action="store_true", help="redo packs that are already done")
    s.add_argument("--limit", type=int, default=0, help="first N models of each pack only (for trials)")
    s.add_argument("--only", default="", help="regex on the file path inside the pack: re-render matching models only")
    s.add_argument("--keep", action="store_true", help="keep the work folder (for debugging)")
    s.add_argument("--device", choices=["auto", "cuda", "mps", "cpu"], help="override the device setting")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("serve", help="browser page + search API")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--open", action="store_true", help="open the page in the default browser")
    s.set_defaults(func=cmd_serve)

    for name, fn in (("find", cmd_find), ("similar", cmd_similar)):
        s = sub.add_parser(name, help="search by description (many queries at once)" if name == "find"
                           else "models that look like one from a search result")
        if name == "find":
            s.add_argument("queries", nargs="+", help='descriptions, e.g. "wooden barrel" "goblin hut"')
            s.add_argument("--pack", help="only packs whose key contains this text")
            s.add_argument("--min-m", type=float, help="largest side at least this many metres")
            s.add_argument("--max-m", type=float, help="largest side at most this many metres")
            s.add_argument("--by", choices=["both", "image", "name"], default="both")
        else:
            s.add_argument("pack", help="the 'pack' field of a result")
            s.add_argument("model", help="the 'model' field of a result (or the model's name)")
        s.add_argument("-k", type=int, default=8, help="results per query")
        s.add_argument("--json", action="store_true", help="JSON output")
        s.add_argument("--port", type=int, default=0)
        s.set_defaults(func=fn)

    s = sub.add_parser("doctor", help="check Blender, GPU / torch, settings, disk space")
    s.add_argument("--fetch-model", action="store_true", help="download the SigLIP weights now if missing")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("config", help="show or change settings")
    s.add_argument("--add-library", nargs="+", metavar="FOLDER")
    s.add_argument("--remove-library", nargs="+", metavar="FOLDER")
    s.add_argument("--set", nargs="+", metavar="KEY=VALUE", help="e.g. --set output=D:/aa procs=3")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("tag", help="re-score every model against the tag list")
    s.set_defaults(func=cmd_tag)

    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
