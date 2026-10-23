"""`python -m trader [serve|setup|reset-password|reset-2fa|logout-all|status|watchdog|backtest]`."""

from trader.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
