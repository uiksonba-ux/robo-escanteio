"""Coleta histórica IQ-V20. SOMENTE LEITURA; nenhuma ordem é enviada.
Uso: IQ_EMAIL=... IQ_PASSWORD=... python coletar_backtest_v20.py
Salva CSVs em dados_backtest_v20/ e manifesto.json.
"""
import csv, json, os, time, pathlib, datetime, logging
from iqoptionapi.stable_api import IQ_Option

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("coletor")
OUT=pathlib.Path(os.getenv("BACKTEST_DIR","dados_backtest_v20"))
OUT.mkdir(parents=True,exist_ok=True)
DAYS=90
INTERVAL=60
BATCH=900
PAUSE=float(os.getenv("BACKTEST_PAUSE_SECONDS","1.5"))
END=int(datetime.datetime(2026,10,9,tzinfo=datetime.timezone.utc).timestamp())
START=END-DAYS*86400
BASE=["EURUSD","GBPUSD","EURGBP","USDJPY","AUDUSD","USDCHF","USDCAD","EURJPY","GBPJPY","AUDCAD","AUDJPY","EURCAD","NZDUSD"]
ASSETS=BASE+[x+"-OTC" for x in BASE]
FIELDS=["from","open","close","min","max","volume"]
email=os.getenv("IQ_EMAIL","").strip()
password=os.getenv("IQ_PASSWORD","").strip()
if not email or not password:
    raise SystemExit("Configure IQ_EMAIL e IQ_PASSWORD em variáveis de ambiente, nunca no código.")
api=IQ_Option(email,password)
ok,reason=api.connect()
if not ok: raise SystemExit("Conexão não estabelecida: "+str(reason))
api.change_balance("PRACTICE")
manifest={"start_utc":START,"end_utc":END,"timeframe_seconds":INTERVAL,"payout_historico":"nao_disponivel","assets":{}}
for asset in ASSETS:
    path=OUT/(asset+".csv")
    if path.exists() and path.stat().st_size>100:
        log.info("Arquivo existente, pulando %s",asset)
        manifest["assets"][asset]={"status":"arquivo_existente","arquivo":str(path)}
        continue
    cursor=END
    rows={}
    errors=0
    while cursor>=START:
        try:
            candles=api.get_candles(asset,INTERVAL,BATCH,cursor)
            if not candles: break
            oldest=cursor
            for x in candles:
                t=int(x.get("from",0))
                if START<=t<END:
                    rows[t]={k:x.get(k,"") for k in FIELDS}
                if t and t<oldest: oldest=t
            if oldest>=cursor: break
            cursor=oldest-1
            errors=0
            if len(rows)%9000<900: log.info("%s: %s candles",asset,len(rows))
            time.sleep(PAUSE)
        except Exception as exc:
            errors+=1
            log.warning("%s erro %s/3: %s",asset,errors,type(exc).__name__)
            if errors>=3: break
            time.sleep(5*errors)
    if rows:
        with path.open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows[t] for t in sorted(rows))
    manifest["assets"][asset]={"status":"ok" if rows else "sem_dados","candles":len(rows),"arquivo":str(path) if rows else None,"cobertura_inicio":min(rows) if rows else None,"cobertura_fim":max(rows) if rows else None}
    (OUT/"manifesto.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding="utf-8")
    log.info("%s: %s candles salvos",asset,len(rows))
log.info("Coleta finalizada. Arquivos em %s",OUT)
