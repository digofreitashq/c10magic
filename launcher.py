"""
Ponto de entrada do C10 Magic como programa único (.exe).

Prepara o banco, sobe o servidor local, abre o navegador e fica rodando até
a janela ser fechada. Também funciona fora do .exe:  python launcher.py
"""

import os
import socket
import sys
import threading
import time
import webbrowser

PREFERRED_PORT = 8000
HOST = "127.0.0.1"


def free_port(preferred: int) -> int:
    """A porta preferida, ou a primeira livre logo depois dela."""

    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((HOST, port)) != 0:
                return port

    raise RuntimeError("Nenhuma porta livre encontrada.")


def wait_until_up(port: int, timeout: float = 60) -> bool:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.25)

    return False


def main() -> int:
    # Saída por linha: as mensagens aparecem na hora, mesmo redirecionadas
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "c10magic.settings")

    import django
    from django.conf import settings
    from django.core.management import call_command

    django.setup()

    print("C10 Magic")
    print("=" * 40)
    print(f"Dados em: {settings.DATA_DIR}")

    # Cria / atualiza o banco (primeira execução e versões novas)
    call_command("migrate", interactive=False, verbosity=0)

    port = free_port(PREFERRED_PORT)
    url = f"http://{HOST}:{port}/"

    def open_browser():
        if wait_until_up(port):
            webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    print(f"Abrindo {url}")
    print("Feche esta janela (ou aperte Ctrl+C) para encerrar o programa.")
    print()

    try:
        # --noreload: o recarregador reiniciaria o processo (não funciona no .exe)
        call_command("runserver", f"{HOST}:{port}", use_reloader=False, skip_checks=True, verbosity=1)
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    # Necessário para o PyInstaller em Windows se algo usar multiprocessing
    import multiprocessing

    multiprocessing.freeze_support()

    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - mostra o erro antes da janela fechar
        print(f"\nErro: {exc}")
        input("Aperte Enter para fechar...")
        sys.exit(1)
