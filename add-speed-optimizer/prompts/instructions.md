# Инструкции: Добавление адаптивного оптимизатора скорости парсинга

## Цель

Перенести адаптивный оптимизатор скорости из `pulscen_parser` в новый проект парсинга на Scrapy. Оптимизатор автоматически подбирает `concurrent_requests` и `download_delay`, пробует ускорение и откатывает при деградации.

---

## Шаг 1: Разведка целевого проекта

Прочитай структуру нового проекта и ответь на вопросы:

1. Где лежат настройки Scrapy? (обычно `settings.py` или `.env`)
2. Какие параметры управляют скоростью? (`CONCURRENT_REQUESTS`, `DOWNLOAD_DELAY` и т.п.)
3. Есть ли supervisor/цикл мониторинга? (файл, который периодически вызывается)
4. Как отслеживаются ошибки? (логи, БД, метрики)
5. Как перезапускается парсер? (subprocess, systemd, scrapy crawl)

---

## Шаг 2: Создать модуль оптимизатора

Создай файл `<project>/supervisor/optimizer.py` (или аналогичный путь по структуре проекта).

### Шаблон optimizer.py

```python
"""
Адаптивный оптимизатор скорости парсинга.

Логика:
  - Каждые OPTIMIZE_EVERY_CYCLES циклов без ошибок — пробует шаг ускорения
  - Период наблюдения подбирается автоматически по волатильности скорости:
      CV (коэффициент вариации) < 10% → 2 цикла — скорость стабильна
      CV 10-25%                  → 4 цикла
      CV > 25%                   → 6 циклов — скорость сильно колеблется
  - Если скорость не упала и error_rate не вырос — фиксирует новые настройки
  - Иначе откатывает к предыдущим настройкам
  - Не оптимизирует если throttle активен или error_rate > порога
"""
import logging
import math

logger = logging.getLogger("supervisor")

# --- Настраиваемые константы ---
OPTIMIZE_EVERY_CYCLES = 6    # минимум чистых циклов перед попыткой
ERROR_DEGRADE_THRESH  = 0.03 # +3% error_rate = деградация
DELAY_STEP            = 0.3  # шаг снижения задержки (сек)
CONCURRENCY_STEP      = 2    # шаг увеличения concurrency
DELAY_MIN_FLOOR       = 0.5  # меньше этого не снижаем
CONCURRENCY_MAX_CAP   = 16   # больше этого не поднимаем
MAX_ERROR_RATE        = 0.15 # порог error rate (15%)

# Границы адаптивного периода наблюдения
OBSERVE_MIN  = 2
OBSERVE_MED  = 4
OBSERVE_MAX  = 6
CV_LOW       = 0.10
CV_HIGH      = 0.25

# --- Внутреннее состояние ---
_opt = {
    "phase":            "idle",
    "observe_count":    0,
    "observe_target":   0,
    "baseline_speed":   0.0,
    "baseline_error":   0.0,
    "before_config":    None,
    "after_config":     None,
    "clean_cycles":     0,
    "total_attempts":   0,
    "total_successes":  0,
    "total_rollbacks":  0,
    "last_cv":          0.0,
    "speed_history":    [],  # локальная история скоростей
    "throttle_active":  False,
    "total_checks":     0,
}


def record_speed(speed: float):
    """Добавить текущую скорость в историю (вызывать каждый цикл)."""
    if speed >= 0:
        _opt["speed_history"].append(speed)
        if len(_opt["speed_history"]) > 50:
            _opt["speed_history"] = _opt["speed_history"][-50:]


def _avg_speed():
    h = _opt["speed_history"]
    if not h:
        return 0.0
    return sum(h) / len(h)


def _calc_cv():
    h = _opt["speed_history"][-10:]
    if len(h) < 3:
        return CV_HIGH
    mean = _avg_speed()
    if mean == 0:
        return CV_HIGH
    variance = sum((x - mean) ** 2 for x in h) / len(h)
    std = math.sqrt(variance)
    return std / mean


def _adaptive_observe_cycles():
    cv = _calc_cv()
    _opt["last_cv"] = cv
    if cv < CV_LOW:
        cycles, label = OBSERVE_MIN, "стабильная"
    elif cv < CV_HIGH:
        cycles, label = OBSERVE_MED, "умеренная"
    else:
        cycles, label = OBSERVE_MAX, "высокая"
    logger.info(
        f"Оптимизатор: волатильность CV={cv:.1%} ({label}) → "
        f"наблюдение {cycles} циклов ({cycles * 5} мин)"
    )
    return cycles


# --- Адаптировать эти функции под свой проект ---

def read_current_config() -> dict:
    """
    АДАПТИРОВАТЬ: Читает текущие параметры скорости.
    Должен вернуть dict с ключами:
      - concurrent_requests: int
      - download_delay_min: float
      - download_delay_max: float
    """
    # Пример: читать из файла settings.py через regex
    # Или из переменных окружения, БД, redis и т.д.
    raise NotImplementedError("Реализуй read_current_config() под свой проект")


def apply_config(concurrent_requests: int, delay_min: float, delay_max: float):
    """
    АДАПТИРОВАТЬ: Применяет новые параметры скорости.
    Способы:
      - записать в settings.py / .env файл
      - обновить redis/memcached ключ
      - записать в конфиг БД
      - передать через IPC в запущенный процесс
    """
    raise NotImplementedError("Реализуй apply_config() под свой проект")


def restart_parser(workers: int = None, batch_size: int = None):
    """
    АДАПТИРОВАТЬ: Перезапускает парсер после изменения конфига.
    Способы:
      - subprocess.Popen(['scrapy', 'crawl', 'spider_name'])
      - systemctl restart service_name
      - os.kill(pid, signal.SIGTERM) + запуск нового процесса
    """
    raise NotImplementedError("Реализуй restart_parser() под свой проект")


# --- Ядро оптимизатора (не менять) ---

def _try_speedup() -> bool:
    config = read_current_config()
    if not config:
        return False

    new_delay = round(max(DELAY_MIN_FLOOR, config["download_delay_min"] - DELAY_STEP), 2)
    new_cr    = min(CONCURRENCY_MAX_CAP, config["concurrent_requests"] + CONCURRENCY_STEP)

    if new_delay == config["download_delay_min"] and new_cr == config["concurrent_requests"]:
        logger.info("Оптимизатор: уже на максимальных параметрах, пропускаем")
        return False

    new_delay_max = round(max(new_delay + 1.0, config["download_delay_max"] - DELAY_STEP), 2)

    _opt["before_config"] = dict(config)
    _opt["after_config"]  = {
        "concurrent_requests": new_cr,
        "download_delay_min":  new_delay,
        "download_delay_max":  new_delay_max,
    }

    logger.info(
        f"Оптимизатор: пробую ускорение — "
        f"concurrent {config['concurrent_requests']}→{new_cr}, "
        f"delay {config['download_delay_min']}→{new_delay}с"
    )
    apply_config(new_cr, new_delay, new_delay_max)
    return True


def _rollback():
    if not _opt["before_config"]:
        return
    c = _opt["before_config"]
    logger.warning(
        f"Оптимизатор: откат — "
        f"concurrent {_opt['after_config']['concurrent_requests']}→{c['concurrent_requests']}, "
        f"delay {_opt['after_config']['download_delay_min']}→{c['download_delay_min']}с"
    )
    apply_config(c["concurrent_requests"], c["download_delay_min"], c["download_delay_max"])
    _opt["total_rollbacks"] += 1


def tick(error_rate: float, workers: int = None, batch_size: int = None):
    """
    Вызывается каждый цикл мониторинга.

    Параметры:
      error_rate  — доля ошибочных запросов (0.0–1.0)
      workers     — количество воркеров (для перезапуска), если применимо
      batch_size  — размер батча (для перезапуска), если применимо
    """
    _opt["total_checks"] += 1
    current_speed = _avg_speed()

    # Считаем чистые циклы
    if error_rate < MAX_ERROR_RATE * 0.5:
        _opt["clean_cycles"] += 1
    else:
        _opt["clean_cycles"] = 0

    # --- Фаза наблюдения ---
    if _opt["phase"] == "observing":
        _opt["observe_count"] += 1
        target = _opt["observe_target"]

        if _opt["observe_count"] < target:
            logger.info(
                f"Оптимизатор: наблюдение {_opt['observe_count']}/{target} "
                f"(CV={_opt['last_cv']:.1%}) — "
                f"скорость {current_speed:.1f} (было {_opt['baseline_speed']:.1f}), "
                f"ошибки {error_rate:.1%} (было {_opt['baseline_error']:.1%})"
            )
            return

        speed_delta = (current_speed - _opt["baseline_speed"]) / max(_opt["baseline_speed"], 1)
        error_delta = error_rate - _opt["baseline_error"]
        improved    = speed_delta >= -0.05 and error_delta <= ERROR_DEGRADE_THRESH

        if improved:
            logger.info(
                f"Оптимизатор: ✓ УСПЕХ — "
                f"скорость {_opt['baseline_speed']:.1f}→{current_speed:.1f} ({speed_delta:+.1%}), "
                f"ошибки {error_rate:.1%}. Оставляю новые настройки."
            )
            _opt["total_successes"] += 1
        else:
            logger.warning(
                f"Оптимизатор: ✗ ОТКАТ — "
                f"скорость {_opt['baseline_speed']:.1f}→{current_speed:.1f} ({speed_delta:+.1%}), "
                f"ошибки {error_rate:.1%}"
            )
            _rollback()
            restart_parser(workers, batch_size)

        _opt["phase"]         = "idle"
        _opt["observe_count"] = 0
        _opt["observe_target"] = 0
        return

    # --- Фаза ожидания ---
    if _opt["phase"] == "idle":
        if _opt.get("throttle_active"):
            return
        if error_rate >= MAX_ERROR_RATE * 0.5:
            return
        if _opt["clean_cycles"] < OPTIMIZE_EVERY_CYCLES:
            return

        _opt["total_attempts"]  += 1
        _opt["baseline_speed"]   = current_speed
        _opt["baseline_error"]   = error_rate
        _opt["clean_cycles"]     = 0
        _opt["observe_target"]   = _adaptive_observe_cycles()

        applied = _try_speedup()
        if applied:
            _opt["phase"] = "observing"
            restart_parser(workers, batch_size)

    # Статистика раз в 12 циклов
    if _opt["total_checks"] % 12 == 0 and _opt["total_attempts"] > 0:
        logger.info(
            f"Оптимизатор: попыток={_opt['total_attempts']}, "
            f"успехов={_opt['total_successes']}, "
            f"откатов={_opt['total_rollbacks']}, "
            f"чистых циклов={_opt['clean_cycles']}, "
            f"CV={_opt['last_cv']:.1%}"
        )


def get_stats() -> dict:
    """Вернуть текущее состояние оптимизатора для отчёта/логов."""
    return {
        "phase":          _opt["phase"],
        "observe_count":  _opt["observe_count"],
        "observe_target": _opt["observe_target"],
        "clean_cycles":   _opt["clean_cycles"],
        "last_cv":        round(_opt["last_cv"], 3),
        "total_attempts": _opt["total_attempts"],
        "total_successes":_opt["total_successes"],
        "total_rollbacks":_opt["total_rollbacks"],
        "baseline_speed": _opt["baseline_speed"],
        "before_config":  _opt["before_config"],
        "after_config":   _opt["after_config"],
    }
```

---

## Шаг 3: Реализовать 3 обязательных функции

### `read_current_config()` — примеры реализации

**Вариант A: читать из scraper/settings.py через regex**
```python
import re, pathlib

SETTINGS_FILE = pathlib.Path(__file__).parent.parent / "scraper" / "settings.py"

def read_current_config() -> dict:
    text = SETTINGS_FILE.read_text()
    def extract(pattern, cast):
        m = re.search(pattern, text)
        return cast(m.group(1)) if m else None
    return {
        "concurrent_requests": extract(r'CONCURRENT_REQUESTS\s*=\s*(\d+)', int),
        "download_delay_min":  extract(r'DOWNLOAD_DELAY\s*=\s*([\d.]+)', float),
        "download_delay_max":  extract(r'RANDOMIZE_DOWNLOAD_DELAY.*?max=([\d.]+)', float)
                               or extract(r'DOWNLOAD_DELAY\s*=\s*([\d.]+)', float),
    }
```

**Вариант B: читать из .env файла**
```python
import re, pathlib

ENV_FILE = pathlib.Path(__file__).parent.parent / ".env"

def read_current_config() -> dict:
    text = ENV_FILE.read_text()
    def extract(key, cast, default):
        m = re.search(rf'^{key}\s*=\s*([\d.]+)', text, re.MULTILINE)
        return cast(m.group(1)) if m else default
    return {
        "concurrent_requests": extract("CONCURRENT_REQUESTS", int, 8),
        "download_delay_min":  extract("DOWNLOAD_DELAY_MIN", float, 1.5),
        "download_delay_max":  extract("DOWNLOAD_DELAY_MAX", float, 2.5),
    }
```

### `apply_config()` — примеры реализации

**Вариант A: перезаписать settings.py**
```python
def apply_config(concurrent_requests, delay_min, delay_max):
    text = SETTINGS_FILE.read_text()
    text = re.sub(r'(CONCURRENT_REQUESTS\s*=\s*)\d+', rf'\g<1>{concurrent_requests}', text)
    text = re.sub(r'(DOWNLOAD_DELAY\s*=\s*)[\d.]+', rf'\g<1>{delay_min}', text)
    SETTINGS_FILE.write_text(text)
```

**Вариант B: перезаписать .env**
```python
def apply_config(concurrent_requests, delay_min, delay_max):
    text = ENV_FILE.read_text()
    text = re.sub(r'^(CONCURRENT_REQUESTS\s*=\s*)[\d.]+', rf'\g<1>{concurrent_requests}', text, flags=re.MULTILINE)
    text = re.sub(r'^(DOWNLOAD_DELAY_MIN\s*=\s*)[\d.]+', rf'\g<1>{delay_min}', text, flags=re.MULTILINE)
    text = re.sub(r'^(DOWNLOAD_DELAY_MAX\s*=\s*)[\d.]+', rf'\g<1>{delay_max}', text, flags=re.MULTILINE)
    ENV_FILE.write_text(text)
```

### `restart_parser()` — примеры реализации

**Вариант A: через subprocess**
```python
import subprocess, signal, os

_crawl_process = None

def restart_parser(workers=None, batch_size=None):
    global _crawl_process
    if _crawl_process and _crawl_process.poll() is None:
        _crawl_process.terminate()
        _crawl_process.wait(timeout=10)

    cmd = ["python", "-m", "scrapy", "crawl", "my_spider"]
    _crawl_process = subprocess.Popen(cmd, cwd="/path/to/project")
    logger.info(f"Оптимизатор: перезапустил парсер (PID={_crawl_process.pid})")
```

**Вариант B: через systemd**
```python
import subprocess

SERVICE_NAME = "my-parser.service"

def restart_parser(workers=None, batch_size=None):
    subprocess.run(["systemctl", "restart", SERVICE_NAME], check=True)
    logger.info(f"Оптимизатор: перезапустил {SERVICE_NAME}")
```

---

## Шаг 4: Интегрировать в цикл мониторинга

Найди или создай цикл мониторинга (запускается каждые 5 минут через cron/systemd/while-loop).

```python
import optimizer

# В начале каждого цикла:
current_speed = get_items_per_minute()  # адаптировать под свой проект
optimizer.record_speed(current_speed)

error_rate = get_error_rate()  # адаптировать под свой проект

# Запустить оптимизатор (не в период throttle):
if not throttle_is_active():
    optimizer.tick(error_rate)

# Сохранить статистику в отчёт:
report["optimizer"] = optimizer.get_stats()
```

---

## Шаг 5: Настроить константы под новый сайт

Отредактируй константы в `optimizer.py` под характеристики нового сайта:

| Константа | Рекомендация | Как выбрать |
|-----------|--------------|-------------|
| `OPTIMIZE_EVERY_CYCLES` | 6 (30 мин) | Больше для нестабильных сайтов |
| `DELAY_MIN_FLOOR` | 0.5с | Минимальная безопасная задержка |
| `CONCURRENCY_MAX_CAP` | 16 | Зависит от proxy-пула |
| `MAX_ERROR_RATE` | 0.15 (15%) | Порог срабатывания throttle |
| `DELAY_STEP` | 0.3с | Меньше = осторожнее |
| `CONCURRENCY_STEP` | 2 | Меньше = осторожнее |

**Для агрессивного сайта (защищённый, банит быстро):**
```python
DELAY_MIN_FLOOR = 1.5
CONCURRENCY_MAX_CAP = 8
DELAY_STEP = 0.2
OPTIMIZE_EVERY_CYCLES = 12
```

**Для слабозащищённого сайта:**
```python
DELAY_MIN_FLOOR = 0.3
CONCURRENCY_MAX_CAP = 32
DELAY_STEP = 0.5
OPTIMIZE_EVERY_CYCLES = 4
```

---

## Шаг 6: Проверить интеграцию

После добавления — убедись что:

1. `optimizer.py` импортируется без ошибок
2. `read_current_config()` возвращает корректные значения
3. `apply_config()` действительно меняет параметры
4. `restart_parser()` не падает
5. `optimizer.tick()` вызывается в цикле мониторинга
6. Логи содержат сообщения вида `"Оптимизатор: чистых циклов X/6"`

---

## Примечание: источник

Оптимизатор скопирован из `/root/pulscen_parser/supervisor/optimizer.py`.
Оригинальная интеграция: `supervisor/cycle.py` (этап H), `supervisor/actions.py`, `supervisor/report.py`.
