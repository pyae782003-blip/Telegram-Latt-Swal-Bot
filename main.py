"""
Multi-Tenant Telegram Bot Cloud Application Entry Point
Supports PostgreSQL (Supabase / Neon / Railway / Fly.io) and local SQLite fallback.
Initializes database schema, executes the Startup Recovery Loader for all
subscription admin bot instances in the background, and runs the master Telegram bot.
"""
from bot import main, startup_recovery_loader, client_bot_manager

if __name__ == "__main__":
    main()
