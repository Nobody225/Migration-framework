#!/usr/bin/env python3
"""
run_api.py — Orange Cloud Migration Platform
Compatible Flask 3.x + Werkzeug 3.x (threading mode, NO eventlet)
"""
import argparse, logging, os, sys
sys.path.insert(0, os.path.dirname(__file__))

def _find_config():
    local   = os.path.join("config", "config.local.yaml")
    default = os.path.join("config", "config.yaml")
    return local if os.path.exists(local) else default

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=_find_config())
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--port",   type=int, default=8080)
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"""
{'─'*52}
  Orange Cloud Migration Platform
{'─'*52}
  Config    : {args.config}
  Dashboard : http://localhost:{args.port}/
  Login     : http://localhost:{args.port}/login
  Health    : http://localhost:{args.port}/api/v1/health
{'─'*52}
  Compte admin : Gos_Cloud / DomSys#gos26
{'─'*52}
""")

    from src.api.app import create_app
    app, socketio = create_app(args.config)

    # ⚠ threading mode — PAS d'eventlet, compatible Werkzeug 3.x
    socketio.run(app, host=args.host, port=args.port,
                 debug=args.debug, use_reloader=False)

if __name__ == "__main__":
    main()
