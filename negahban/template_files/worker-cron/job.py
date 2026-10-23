"""The periodic job. Runs from the in-process scheduler (worker.py) or from a Liara cron entry: python /app/job.py"""
import os
import time
from pathlib import Path

DATA = Path(os.getenv('DATA_DIR', '{{data_dir}}'))


def run():
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{{app_name}}] job ran at {stamp}', flush=True)
    if DATA.is_dir():
        (DATA / 'last_run.txt').write_text(stamp)


if __name__ == '__main__':
    run()
