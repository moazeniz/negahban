"""Background worker: a tiny health endpoint for Liara plus an in-process scheduler for job.run().
Set INTERNAL_SCHEDULER=0 when the cron entries of liara.json run the job instead."""
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import job

STATE = {'runs': 0, 'last': None, 'last_error': None}


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f'ok runs={STATE["runs"]} last={STATE["last"]}'.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def scheduler():
    every = max(10, int(os.getenv('JOB_INTERVAL_SECONDS', '300')))
    while True:
        try:
            job.run()
            STATE['runs'] += 1
            STATE['last'] = time.strftime('%H:%M:%S')
        except Exception:
            STATE['last_error'] = traceback.format_exc()[-500:]
            print(STATE['last_error'], flush=True)
        time.sleep(every)


if __name__ == '__main__':
    if os.getenv('INTERNAL_SCHEDULER', '1') == '1':
        threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '{{port}}'))), Health).serve_forever()
