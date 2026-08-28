import typer

from .core import Alert, Channel, Monitor, SessionLocal, init_db, run_monitor

app = typer.Typer(help="Pet Price Guard")


@app.command("add-monitor")
def add_monitor(brand: str, product: str, sku: str = "", channel: str = typer.Option(...), url: str = typer.Option(...), floor: float = typer.Option(...), schedule: str = ""):
    init_db()
    with SessionLocal() as db:
        monitor = Monitor(brand=brand, product=product, sku=sku or None, floor_price=floor, schedule=schedule or None)
        monitor.channels = [Channel(name=channel, url=url)]
        db.add(monitor)
        db.commit()
        db.refresh(monitor)
        typer.echo(f"Created monitor {monitor.id}")


@app.command()
def run(monitor_id: int):
    init_db()
    typer.echo(run_monitor(monitor_id))


@app.command()
def report():
    init_db()
    with SessionLocal() as db:
        typer.echo({"monitors": db.query(Monitor).count(), "alerts_open": db.query(Alert).filter(Alert.status == "open").count(), "alerts_total": db.query(Alert).count()})


if __name__ == "__main__":
    app()
