"""
RegisterHub CLI Main Entrypoint.
The Modern Headless Account & Form Automation Framework.

Import policy: heavyweight modules that pull in Playwright are imported lazily,
inside the functions that actually need a browser. This keeps dependency-free
commands -- most importantly `doctor` -- runnable on a machine where Playwright
has not been installed yet. A diagnostic tool that crashes before it can report
what is missing is useless.
"""
import sys
import time
import json
import argparse
from pathlib import Path


def load_config() -> dict:
    cfg_path = Path("config.json")
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"browser": {"headless": False}, "output_dir": "./results"}


def run_doctor():
    """Run the environment diagnostic suite.

    Delegates to core.doctor, which performs real executable verification
    (browser launch, OpenCV native call, filesystem write, network probe)
    rather than merely confirming that modules import.
    """
    from core.doctor import run_doctor as _doctor
    cfg = load_config()
    output_dir = cfg.get("output_dir", "./results")
    return _doctor(output_dir=output_dir)


def inspect_url(target_url: str, cdp_endpoint: str = None):
    from playwright.sync_api import sync_playwright
    from core.stealth import apply_stealth_to_context, connect_fingerprint_browser
    from core.inspector import FormInspector
    from core.identity import generate_identity
    from core.pilot import human_type, smart_agree_tos
    import os
    print(f"[*] Navigating to {target_url} for heuristic form inspection...")
    
    cfg = load_config()
    proxy_server = cfg.get("browser", {}).get("proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    
    with sync_playwright() as p:
        if cdp_endpoint:
            print(f"[*] Connecting to fingerprint browser at {cdp_endpoint}...")
            browser = connect_fingerprint_browser(p, cdp_endpoint)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check"
            ]
            launch_kwargs = {
                "headless": False,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation"]
            }
            if proxy_server:
                launch_kwargs["proxy"] = {"server": proxy_server}
                
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                browser = p.chromium.launch(**launch_kwargs)

            # Let official Google Chrome use its native User-Agent and Client Hints to guarantee tri-consistency
            context = browser.new_context(
                no_viewport=True
            )

        apply_stealth_to_context(context)
        page = context.new_page()
        try:
            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            inspector = FormInspector(page)
            fields = inspector.detect_inputs()
            
            print("\n[+] Heuristic Detection Results:")
            for k, v in fields.items():
                status = v if v else "Not found / Not visible"
                print(f"    - {k:18s}: {status}")

            if any(fields.values()):
                print("\n[*] Generating synthetic multi-field identity and testing autofill...")
                ident = generate_identity()
                if fields["email"]:
                    print(f"    -> Typing Email: {ident['email']}")
                    human_type(page, fields["email"], ident["email"])
                if fields["password"]:
                    print(f"    -> Typing Password: {ident['password']}")
                    human_type(page, fields["password"], ident["password"])
                if fields["confirm_password"]:
                    print(f"    -> Typing Confirm Password: {ident['password']}")
                    human_type(page, fields["confirm_password"], ident["password"])
                if fields["username"]:
                    print(f"    -> Typing Username: {ident['username']}")
                    human_type(page, fields["username"], ident["username"])
                if fields["fullname"]:
                    print(f"    -> Typing Fullname: {ident['full_name']}")
                    human_type(page, fields["fullname"], ident["full_name"])

                # Auto agree terms if checkbox found
                smart_agree_tos(page)
                
                print("\n[+] Form autofill finished with humanized typing rhythms.")
                print("[*] Browser will remain open for 10 seconds for visual inspection.")
                time.sleep(10)
            else:
                print("[-] Could not detect standard input selectors heuristically.")
        except Exception as e:
            print(f"[-] Inspection error: {e}")
        finally:
            browser.close()


def run_scaffold(site_name: str, target_url: str = None, *, overwrite: bool = False):
    from core.scaffold import scaffold_site
    print(f"[*] Scaffolding new adapter for '{site_name}'...")
    detected = {}
    if target_url:
        print(f"[*] Pre-inspecting '{target_url}' to extract input selectors...")
        try:
            from playwright.sync_api import sync_playwright
            from core.stealth import apply_stealth_to_context
            from core.inspector import FormInspector
            with sync_playwright() as p:
                launch_kwargs = {
                    "headless": True,
                    "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
                    "ignore_default_args": ["--enable-automation"]
                }
                try:
                    browser = p.chromium.launch(channel="chrome", **launch_kwargs)
                except Exception:
                    browser = p.chromium.launch(**launch_kwargs)
                context = browser.new_context()
                try:
                    apply_stealth_to_context(context)
                    page = context.new_page()
                    page.goto(target_url, timeout=25000, wait_until="domcontentloaded")
                    inspector = FormInspector(page)
                    detected = inspector.detect_inputs()
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass
            print("[+] Form fields successfully detected and pre-assembled into scaffold!")
        except Exception as e:
            print(f"[!] Warning: Pre-inspection timed out or failed ({e}), generating standard template instead.")

    try:
        out_path = scaffold_site(site_name, target_url, detected, overwrite=overwrite)
    except FileExistsError as exc:
        print(f"[-] {exc}")
        sys.exit(1)
    print(f"\n[+] Production scaffold successfully created at: {out_path.resolve()}")
    print(f"[*] You can now test and run it immediately:")
    print(f"    python main.py --run {site_name.lower()}\n")


def main():
    parser = argparse.ArgumentParser(description="RegisterHub Automation Engine")
    parser.add_argument("--run", type=str, default=None, help="Target site adapter name (e.g. demo, automation_exercise)")
    parser.add_argument("--list", action="store_true", help="List all available site adapters")
    parser.add_argument("--doctor", action="store_true", help="Run environment diagnostics (same as `python main.py doctor`)")
    parser.add_argument("--scaffold", type=str, default=None, help="Scaffold a new site adapter template (e.g. twitter, github)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing adapter file when used with --scaffold (saves .bak backup)")
    parser.add_argument("--count", type=int, default=1, help="Number of accounts to generate")
    parser.add_argument("--url", type=str, default=None, help="Inspect URL or target URL for scaffolding")
    parser.add_argument("--cdp", type=str, default=None, help="Connect to an external Fingerprint/Anti-Detect browser CDP (e.g. http://127.0.0.1:9222)")
    parser.add_argument("--export", type=str, default=None, choices=["csv", "json", "bitwarden"], help="Export accounts to csv, json or bitwarden format")
    parser.add_argument("--auto-layout", action="store_true", help="Auto-generate symmetric window layouts (1/2/4x2/6x2) for the current display work area")
    parser.add_argument("--quad-mode", choices=["edge_2x2", "centered_compact"], help="Switch 4-window preset mode with --auto-layout")
    parser.add_argument("--hex-mode", choices=["edge_3x2", "wing_cascade"], help="Switch 6-window preset mode with --auto-layout")
    parser.add_argument("--demo-layouts", action="store_true", help="Preview all calibrated/auto-generated window layouts in sequence")
    parser.add_argument("--demo-sec", type=int, default=10, help="Seconds to hold each layout during --demo-layouts (default: 10)")
    parser.add_argument("--calibrate", action="store_true", help="Launch interactive window geometry & anti-clipping zoom calibrator")
    parser.add_argument("--calibrate-url", type=str, default="", help="Optional URL to load inside --calibrate")
    parser.add_argument("-c", "--concurrency", type=int, default=1, help="Window count for --calibrate (1/2/4/6)")
    parser.add_argument("--web", action="store_true", help="Launch RegisterHub Lite Web Workbench (zero-dependency stdlib server)")
    parser.add_argument("--port", type=int, default=18886, help="Port for --web Lite Workbench (default: 18886)")
    parser.add_argument("--open", action="store_true", help="Auto-open default browser once --web /health is ready")
    parser.add_argument("--from-har", type=str, default=None, help="Extract signup/auth API steps & Python skeleton from a .har capture file")
    parser.add_argument("--site", type=str, default="", help="Optional site slug for --from-har")
    parser.add_argument("--har-out", type=str, default="", help="Optional output path to save the Python skeleton generated by --from-har")
    parser.add_argument("command", nargs="?", default=None, help="Subcommand like 'doctor' or 'export'")

    args = parser.parse_args()

    if args.web or args.command == "web":
        from web.server import start_server
        start_server(host="127.0.0.1", port=args.port, open_browser=bool(args.open))
        return

    if args.from_har:
        from web.server import _parse_har_community
        har_path = Path(args.from_har)
        if not har_path.is_file():
            print(f"[-] HAR file not found: {har_path}")
            sys.exit(1)
        try:
            har_text = har_path.read_text(encoding="utf-8", errors="replace")
            res = _parse_har_community(har_text, site_name=(args.site or "").strip())
        except Exception as exc:
            print(f"[-] Failed to parse HAR: {exc}")
            sys.exit(1)
        print(f"[+] HAR parsed: {har_path.name} -> site='{res['site']}' | origin={res.get('origin') or 'N/A'}")
        print(f"    API steps: {len(res.get('api_steps') or [])} | Signals: {', '.join(res.get('signals') or []) or 'None'}")
        for idx, st in enumerate((res.get("api_steps") or [])[:15], 1):
            print(f"    {idx:2d}. {st['method']:6s} {st['path']} (HTTP {st['status']})")
        if args.har_out:
            out_p = Path(args.har_out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(str(res.get("skeleton") or ""), encoding="utf-8")
            print(f"[+] Protocol skeleton written to: {out_p.resolve()}")
        return

    if args.auto_layout:
        from core.calibrator import auto_generate_layout
        auto_generate_layout(quad_mode=args.quad_mode, hex_mode=args.hex_mode)
        return

    if args.demo_layouts:
        from core.calibrator import demo_layouts
        demo_layouts(hold_sec=args.demo_sec)
        return

    if args.calibrate:
        from core.calibrator import calibrate_window
        if args.concurrency not in (1, 2, 4, 6):
            print(f"[-] Unsupported concurrency '{args.concurrency}' for --calibrate. Supported values: 1, 2, 4, 6")
            sys.exit(1)
        calibrate_window(url=args.calibrate_url or (args.url or ""), total_windows=args.concurrency)
        return

    if args.doctor or args.command == "doctor":
        # Exit non-zero when blocking issues are found, so CI and wrapper
        # scripts can gate on the real environment state.
        ok = run_doctor()
        if not ok:
            sys.exit(1)
        return

    if args.command == "list" or args.list:
        from sites import discover_adapters
        adapters = discover_adapters()
        print("=" * 65)
        print("  RegisterHub - Discovered Site Adapters")
        print("=" * 65)
        for name, cls in sorted(adapters.items()):
            try:
                url_attr = getattr(cls, "target_url", None)
                if callable(url_attr):
                    url = url_attr(None)
                elif isinstance(url_attr, property):
                    url = url_attr.fget(None)
                else:
                    url = str(url_attr) if url_attr else "N/A"
            except Exception:
                url = "N/A"
            display_url = url if len(url) <= 60 else (url[:45] + "... [embedded html]")
            print(f"  * {name:<22} -> {display_url}")
        print("=" * 65)
        print(f"  Total: {len(adapters)} adapters ready to run with 'python main.py --run <name>'")
        print("=" * 65)
        return

    if args.command == "export" or args.export:
        from core.exporter import run_export
        fmt = args.export or "csv"
        try:
            exported_path = run_export(fmt=fmt)
            print(f"[+] Accounts successfully exported to [{fmt.upper()}]: {exported_path.resolve()}")
        except Exception as e:
            print(f"[-] Export error: {e}")
            sys.exit(1)
        return

    if args.scaffold:
        run_scaffold(args.scaffold, target_url=args.url, overwrite=args.force)
        return

    if args.url and not args.run:
        inspect_url(args.url, cdp_endpoint=args.cdp)
        return

    cfg = load_config()
    from core.runner import StandardRunner
    runner = StandardRunner(cfg, cdp_endpoint=args.cdp)

    from sites import discover_adapters
    adapters = discover_adapters()
    if args.run:
        target_name = args.run
    elif args.command:
        target_name = args.command
    else:
        target_name = "demo"
    adapter_name = target_name.lower().replace("-", "_")
    
    if adapter_name in adapters:
        ok_count = runner.execute_adapter(adapters[adapter_name], count=args.count)
        if ok_count <= 0:
            sys.exit(1)
    else:
        available = ", ".join(sorted(adapters.keys()))
        print(f"[-] Unknown adapter or command '{target_name}'. Available discovered adapters: {available}")
        sys.exit(1)


if __name__ == "__main__":
    main()
