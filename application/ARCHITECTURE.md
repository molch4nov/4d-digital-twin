# Архитектура и стек приложения «4D Digital Twin»

## Общая архитектура (схема для draw.io)

### Как построить диаграмму — пошаговая инструкция

В draw.io используй **светлый фон**, контейнеры (Container / Group) для логических слоёв и стрелки для потоков данных.

---

### Слой 1 — Клиент (Frontend)

Нарисуй контейнер **«Браузер (Web-клиент)»**.

Внутри него — блоки:

| Блок | Подпись | Цвет заливки |
|---|---|---|
| React SPA | React 19 + TypeScript + Vite | голубой (#dae8fc) |
| TailwindCSS | TailwindCSS 4 (стили) | голубой |
| Three.js Viewer | 3D-просмотрщик (GLB + 3DGS PLY) | голубой |
| Pan Viewer | Панорамный просмотрщик (360°) | голубой |

Стрелка от блока «React SPA» к блоку «Three.js Viewer» с подписью «рендерит сцену».

---

### Слой 2 — Сервер приложений (Backend)

Рядом (справа или снизу) — контейнер **«FastAPI-сервер (Python 3.13 + Uvicorn)»**.

Внутри — блоки:

| Блок | Подпись | Цвет заливки |
|---|---|---|
| REST API | `/api/v1/tasks/*` — CRUD | зелёный (#d5e8d4) |
| SSE Events | Server-Sent Events — прогресс в реальном времени | зелёный |
| Worker Thread | Фоновый поток-обработчик задач | зелёный |
| Static Files | Раздача фронтенда + результатов | зелёный |
| Pipelines | Диспетчер конвейеров 3D-реконструкции | зелёный |

Стрелки внутри контейнера:
- «REST API» → «SSE Events» (подпись: «публикует события»)
- «Worker Thread» → «REST API» (подпись: «обновляет статус» через commit_and_notify)
- «Worker Thread» → «Pipelines» (подпись: «запускает конвейер»)

---

### Слой 3 — Хранилище (Database & Filesystem)

Ниже — контейнер **«Хранилище»**.

Внутри — блоки:

| Блок | Подпись | Цвет заливки |
|---|---|---|
| SQLite | `backend.db` (задачи, статусы) | оранжевый (#fff2cc) |
| Файловая система | Видео, кадры, результаты (GLB/PLY) | оранжевый |

Стрелки:
- «REST API» ↔ «SQLite» (чтение/запись)
- «Worker Thread» ↔ «Файловая система» (чтение видео, запись результатов)
- «Static Files» → «Файловая система» (раздача файлов)

---

### Слой 4 — Внешние инструменты (System Dependencies)

Слева или снизу — контейнер **«Внешние инструменты 3D-реконструкции»**.

Внутри — блоки:

| Блок | Подпись | Цвет заливки |
|---|---|---|
| ffmpeg | Извлечение кадров, ERP→перспектива | фиолетовый (#e1d5e7) |
| COLMAP | Structure from Motion (SfM) | фиолетовый |
| SphereSfM | COLMAP с SPHERE-моделью камеры | фиолетовый |
| OpenMVG | Альтернативный SfM | фиолетовый |
| OpenMVS | Плотная реконструкция → текстурированный меш | фиолетовый |
| 3DGS | 3D Gaussian Splatting (train.py) | фиолетовый |

Стрелка от «Pipelines» → каждый из блоков с подписью «subprocess / shell-скрипт».

---

### Межслойные связи (главные стрелки)

1. **Клиент → Backend:** HTTP REST (JSON) + SSE (text/event-stream)
2. **Backend → Клиент:** SSE-поток событий (progress, status, result_url)
3. **Backend → Хранилище:** SQL-запросы (SQLAlchemy ORM), файловый ввод-вывод
4. **Backend → Внешние инструменты:** `subprocess.run()` вызывает shell-скрипты (`run_colmap_360.sh`, `run_mvs_from_colmap.sh`, `run_3dgs_from_colmap360.sh`)
5. **Внешние инструменты → Хранилище:** пишут промежуточные и финальные файлы (sparse/0/, dense/, scene.mvs, point_cloud.ply) в `data/results/{task_id}/`

---

### Итоговая схема (чек-лист)

Твоя диаграмма должна содержать:

```
┌──────────────────────────────────────────────────────────┐
│                    БРАУЗЕР (WEB-КЛИЕНТ)                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ React    │  │Three.js  │  │  Pan     │  │Tailwind  │ │
│  │ SPA      │  │ Viewer   │  │ Viewer   │  │CSS 4     │ │
│  └────┬─────┘  └──────────┘  └──────────┘  └──────────┘ │
│       │  HTTP REST + SSE                                   │
└───────┼───────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│              FASTAPI-СЕРВЕР (Python 3.13)                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐    │
│  │ REST API │  │SSE Events│  │  Worker Thread        │    │
│  └────┬─────┘  └──────────┘  │  ┌────────────────┐  │    │
│       │                      │  │   Pipelines     │  │    │
│       │                      │  │ (диспетчер)     │  │    │
│       │                      │  └───────┬────────┘  │    │
│  ┌────┴─────┐                │          │subprocess  │    │
│  │ Static   │                └──────────┼────────────┘    │
│  │ Files    │                           │                 │
│  └──────────┘                           │                 │
└──────┬──────┼───────────────────────────┼─────────────────┘
       │      │                           │
       ▼      ▼                           ▼
┌──────────┐ ┌──────────────────┐  ┌────────────────────────┐
│ SQLite   │ │ Файловая система │  │  ВНЕШНИЕ ИНСТРУМЕНТЫ   │
│ (tasks)  │ │ видео/GLB/PLY    │  │ ffmpeg → COLMAP →      │
│          │ │                  │  │ OpenMVS → 3DGS → GLB   │
└──────────┘ └──────────────────┘  └────────────────────────┘
```

---

## Поток обработки задачи (Data Flow)

1. **Загрузка:** Пользователь загружает видео (MP4/AVI) через React-форму, выбирает тип конвейера → `POST /api/v1/tasks/` (multipart).
2. **Сохранение:** Видео сохраняется в `data/uploads/{uuid}/`. Создаётся запись в SQLite со статусом `PENDING`.
3. **Обработка:** Фоновый поток (`worker.py`) опрашивает БД каждые 2 секунды. При обнаружении `PENDING`-задачи вызывает `task_worker.process_task()`.
4. **Диспетчеризация:** `process_task()` по типу конвейера запускает:
   - `openmvg_openmvs` → извлечение кадров (ffmpeg) → OpenMVG SfM → OpenMVS плотная реконструкция → GLB
   - `sphere_colmap_openmvs` → извлечение кадров → SphereSfM → OpenMVS → GLB
   - `gaussian_splatting` → извлечение кадров → COLMAP SfM → 3D Gaussian Splatting → PLY
   - `colmap360_openmvs` → ERP-кадры → кубические проекции (ffmpeg v360) → COLMAP SfM → OpenMVS → GLB
   - `colmap360_3dgs` → ERP-кадры → кубические проекции → COLMAP SfM → 3DGS → PLY
5. **Прогресс:** На каждом этапе обновляется поле `progress` (0.0–1.0), через `commit_and_notify()` публикуется SSE-событие на фронтенд.
6. **Результат:** Статус `COMPLETED`. Фронтенд получает `result_url` → Three.js рендерит GLB-меш или 3DGS-сплат.
7. **Панорамы (для colmap360):** После реконструкции строится `panorama_index.json` — при клике на точку съёмки в 3D-сцене открывается панорамный просмотрщик с исходным ERP-кадром.

---

## Стек технологий

### Frontend

| Технология | Версия | Назначение |
|---|---|---|
| **React** | 19 | UI-фреймворк, одностраничное приложение (SPA) |
| **TypeScript** | 5.5+ | Статическая типизация |
| **Vite** | 5 | Сборщик и dev-сервер с HMR |
| **TailwindCSS** | 4 | Utility-first CSS-фреймворк |
| **Three.js** | 0.184 | 3D-рендеринг GLB-мешей (OrbitControls, GLTFLoader) |
| **@mkkellogg/gaussian-splats-3d** | 0.4.7 | Визуализация 3D Gaussian Splatting (PLY) |
| **React Router DOM** | 7 | (зарезервирован) Клиентская маршрутизация |

### Backend

| Технология | Версия | Назначение |
|---|---|---|
| **Python** | 3.13 | Язык программирования |
| **FastAPI** | 0.x | Асинхронный REST API-фреймворк |
| **Uvicorn** | — | ASGI-сервер |
| **SQLAlchemy** | 2.0 | ORM для работы с БД |
| **SQLite** | 3 | Реляционная БД (файл `backend.db`) |
| **Pydantic Settings** | — | Конфигурация из `.env`-файла |
| **python-multipart** | — | Обработка multipart-загрузок |
| **Pillow** | — | Ресайз изображений |
| **NumPy** | — | Линейная алгебра, работа с матрицами камер |
| **trimesh** | — | Конвертация PLY в GLB, постобработка мешей |

### Инструменты 3D-реконструкции (системные зависимости)

| Инструмент | Назначение |
|---|---|
| **ffmpeg** | Извлечение кадров из видео, преобразование ERP ↔ кубические проекции (фильтр `v360`) |
| **COLMAP** | Structure from Motion: извлечение признаков (SIFT), сопоставление, bundle adjustment, разреженная реконструкция |
| **SphereSfM** | Форк COLMAP с поддержкой сферической (SPHERE) модели камеры для эквидистантных панорам |
| **OpenMVG** | Альтернативный SfM-конвейер (SfMInit_ImageListing → ComputeFeatures → ComputeMatches → IncrementalSfM) |
| **OpenMVS** | Плотная реконструкция: DensifyPointCloud → ReconstructMesh → RefineMesh → TextureMesh → экспорт GLB |
| **3D Gaussian Splatting** | Метод нового представления (graphdeco/inria): `train.py` — обучение 3D-гауссиан по COLMAP-разреженной реконструкции |
| **xvfb-run** | Виртуальный фреймбуфер для headless GPU-SIFT (COLMAP требует OpenGL-контекст) |

### Методы межпроцессного взаимодействия

| Механизм | Применение |
|---|---|
| **HTTP REST (JSON)** | Клиент ↔ Сервер (CRUD задач, получение результатов) |
| **Server-Sent Events (SSE)** | Сервер → Клиент (потоковый прогресс обработки в реальном времени) |
| **subprocess (Bash)** | Сервер → Внешние инструменты (запуск shell-скриптов конвейеров) |

---

## Ключевые архитектурные решения

1. **Монолит с фоновым воркером.** Единый FastAPI-процесс обслуживает и API, и раздачу статики. Фоновый Python-поток (daemon thread) обрабатывает задачи 3D-реконструкции. Без Celery, Redis, очередей сообщений — минимальная инфраструктура.

2. **SSE вместо WebSocket.** Для real-time-уведомлений используется Server-Sent Events — однонаправленный поток от сервера к клиенту. Это проще WebSocket, не требует дополнительных библиотек и работает через стандартный HTTP.

3. **Shell-обёртки для конвейеров.** Сложные многошаговые процессы (COLMAP → OpenMVS → GLB или COLMAP → 3DGS) вынесены в Bash-скрипты с контрольными точками (resume-from-checkpoint). Python-код запускает их через `subprocess`.

4. **Файловое хранилище результатов.** Все артефакты (видео, кадры, облака точек, меши, логи) хранятся в локальной файловой системе `backend/data/`. Статические файлы раздаются напрямую через FastAPI `StaticFiles` без прокси-сервера.

5. **Гибридный 3D-просмотрщик.** Фронтенд автоматически определяет тип результата (GLB-меш или PLY-сплат) и подключает соответствующий рендерер: Three.js `GLTFLoader` для мешей, `@mkkellogg/gaussian-splats-3d` для гауссиан.

6. **Панорамная привязка.** Для конвейеров COLMAP 360 строится индекс `panorama_index.json`, связывающий позиции камер из SfM с исходными ERP-кадрами. При клике на точку съёмки в 3D-сцене открывается панорамный просмотрщик — это и есть «4D»-аспект (3D-пространство + время/ракурс).

---

## Схема базы данных

```sql
-- Таблица tasks (SQLite, backend.db)
CREATE TABLE tasks (
    id               VARCHAR(36) PRIMARY KEY,   -- UUID
    pipeline_type    VARCHAR(50) NOT NULL,       -- ENUM: openmvg_openmvs, gaussian_splatting,
                                                  --       sphere_colmap_openmvs, colmap360_openmvs,
                                                  --       colmap360_3dgs
    status           VARCHAR(20) DEFAULT 'pending', -- pending, processing, completed, failed
    created_at       DATETIME,
    updated_at       DATETIME,
    original_filename VARCHAR,
    video_path       VARCHAR,
    frames_path      VARCHAR,
    frames_count     INTEGER,
    openmvg_matches_path      VARCHAR,
    openmvg_reconstruction_path VARCHAR,
    openmvs_scene_path        VARCHAR,
    gaussian_model_path       VARCHAR,
    result_path      VARCHAR,
    error_message    VARCHAR,
    progress         FLOAT DEFAULT 0.0,
    extra_data       JSON
);
```

---

## API (основные endpoint'ы)

Префикс: `/api/v1`

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/health` | Проверка работоспособности |
| `POST` | `/tasks/` | Создать задачу (видео + тип конвейера) |
| `GET` | `/tasks/` | Список последних задач (кэш 3с) |
| `GET` | `/tasks/stream` | SSE-поток всех обновлений |
| `GET` | `/tasks/{id}` | Детали задачи |
| `GET` | `/tasks/{id}/stream` | SSE-поток для одной задачи |
| `GET` | `/tasks/{id}/result` | Скачать результат (файл или .tar.gz) |
| `GET` | `/tasks/{id}/video` | Скачать исходное видео |
| `GET` | `/tasks/{id}/log` | Скачать лог обработки |
| `POST` | `/tasks/{id}/restart` | Перезапустить задачу |
| `GET` | `/tasks/{id}/manifest` | Получить панорамный манифест |
| `GET` | `/tasks/{id}/panorama/nearest` | Ближайшая панорама к 3D-координате |
| `GET` | `/files/{id}/{path}` | Раздача статического файла результата |
