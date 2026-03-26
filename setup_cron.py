"""
setup_cron.py — Install the FCR trading crontab automatically.
Run once: python3 setup_cron.py
"""
import subprocess

FCR = "/Users/joshcook/fcr"
PY  = "/usr/bin/python3"

lines = [
    "TZ=America/New_York",
    f"0 8 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_morning.py >> logs/morning.log 2>&1",
    # Stream starts at 09:25 ET so it's live before the first candle closes
    f"25 9 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_stream.py >> logs/stream.log 2>&1",
    f"1 10 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_first_candle.py >> logs/scanner.log 2>&1",
    f"* 10 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
    f"* 11 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
    f"* 12 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
    f"* 13 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
    f"* 14 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
    f"* 9 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    f"* 10 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    f"* 11 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    f"* 12 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    f"* 13 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    f"* 14 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    f"* 15 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    f"* 16 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
]

crontab = "\n".join(lines) + "\n"

result = subprocess.run(["crontab", "-"], input=crontab.encode(), capture_output=True)

if result.returncode == 0:
    print("Crontab installed successfully!")
    print("\nVerifying...")
    verify = subprocess.run(["crontab", "-l"], capture_output=True)
    print(verify.stdout.decode())
else:
    print("Error installing crontab:")
    print(result.stderr.decode())
