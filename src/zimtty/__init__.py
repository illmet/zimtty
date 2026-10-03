def main():
    # Lazy import keeps the search subprocess independent of the UI framework.
    from .app import main as run

    run()

__all__ = ["main"]
