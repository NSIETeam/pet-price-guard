import typer
from .core import Channel, Monitor, SessionLocal, init_db, run_monitor, Alert
app=typer.Typer(help="Pet Price Guard")
@app.command("add-monitor")
def add_monitor(brand:str, product:str, sku:str="", channel:str=typer.Option(...), url:str=typer.Option(...), floor:float=typer.Option(...), schedule:str=""):
    init_db()
    with SessionLocal() as db:
        m=Monitor(brand=brand,product=product,sku=sku or None,floor_price=floor,schedule=schedule or None); m.channels=[Channel(name=channel,url=url)]; db.add(m); db.commit(); db.refresh(m); typer.echo(f"Created monitor {m.id}")
@app.command()
def run(monitor_id:int): init_db(); typer.echo(run_monitor(monitor_id))
@app.command()
def report():
    init_db()
    with SessionLocal() as db: typer.echo({"monitors":db.query(Monitor).count(),"alerts_open":db.query(Alert).filter(Alert.status=="open").count(),"alerts_total":db.query(Alert).count()})
if __name__ == "__main__": app()
