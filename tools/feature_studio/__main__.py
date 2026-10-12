"""Feature Studio command-line operations. Writes require explicit target and flag."""
import argparse
import json
from pathlib import Path
import sys

from .control import STUDIO, StudioStore, inventory, probe, plan
from .github_sync import rollout_preview, rollout_repo, safe_deployment_policy


def main() -> int:
    parser = argparse.ArgumentParser(description="Feature Studio local control plane")
    sub = parser.add_subparsers(dest="action",required=True)
    sub.add_parser("inventory")
    sub.add_parser("rollout-preview")
    install=sub.add_parser("install-form")
    install.add_argument("--repo", required=True)
    install.add_argument("--apply", action="store_true",help="Create draft PR, never merge")
    spec=sub.add_parser("probe")
    spec.add_argument("--spec",required=True,type=Path)
    spec.add_argument("--checkout",required=True,type=Path)
    args=parser.parse_args()
    try:
        if args.action=="inventory": result=inventory()
        elif args.action=="rollout-preview": result=rollout_preview(inventory())
        elif args.action=="install-form":
            if not args.apply:
                result={"repository":args.repo,"mode":"DRY_RUN",
                        "policy":safe_deployment_policy(),
                        "template":".github/ISSUE_TEMPLATE/feature-studio.yml"}
            else:
                result=rollout_repo(args.repo,source=STUDIO.parent/".github"/"ISSUE_TEMPLATE"/"feature-studio.yml")
        else:
            data=json.loads(args.spec.read_text(encoding="utf-8"))
            findings=probe(args.checkout,data)
            result={"discovery":findings,"plan":plan(data,findings)}
        print(json.dumps(result,indent=2))
        return 0
    except (RuntimeError,ValueError,OSError) as exc:
        print(f"Feature Studio operation declined: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
