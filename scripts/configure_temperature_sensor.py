"""Configure one LAN sensor API for both indoor widgets, or manage legacy push sensors."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.indoor_temperature import DEFAULT_SOURCE_URL, TemperatureStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="Room Hub service's existing data directory")
    actions = parser.add_subparsers(dest="action", required=True)
    source = actions.add_parser("set-source", help="Read /api/sensors once for both indoor widgets; no sender token needed")
    source.add_argument("--device-id", default="room-temp-01")
    source.add_argument("--url", default=DEFAULT_SOURCE_URL)
    source.add_argument("--label", default="내 방")
    for action in ("create", "rotate"):
        sub = actions.add_parser(action)
        sub.add_argument("--device-id", required=True)
        sub.add_argument("--label", default="내 방")
    actions.add_parser("list")
    revoke = actions.add_parser("revoke")
    revoke.add_argument("--device-id", required=True)
    args = parser.parse_args()
    try:
        data = Path(args.data_dir)
        if not data.is_absolute() or not data.is_dir():
            raise ValueError("기존 Room Hub 데이터 디렉터리의 절대 경로를 지정하세요.")
        service = TemperatureStore(data)
        if args.action == "set-source":
            result = service.set_source(args.device_id, args.url, args.label)
        elif args.action in {"create", "rotate"}:
            result = service.create_device(args.device_id, args.label, rotate=args.action == "rotate")
            print("발급된 token은 보드의 config.h에만 보관하세요. 채팅/공개 로그에 붙이지 마세요.", file=sys.stderr)
        elif args.action == "list":
            result = {"devices": service.list_devices()}
        else:
            service.revoke(args.device_id)
            result = {"revoked": args.device_id}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
