# Web Interface

Браузерный GUI для AI YouTube Shorts Generator.

## Запуск

1. Установите зависимости:
```bash
pip install -r requirements.txt
```

2. Убедитесь, что настроен `.env` файл с необходимыми API ключами:
```bash
# Для API режима (по умолчанию)
MUAPI_API_KEY=your_muapi_key_here

# Для локального режима
OPENAI_API_KEY=your_openai_key_here
# или
GEMINI_API_KEY=your_gemini_key_here
```

3. Запустите веб-сервер:
```bash
python app.py
```

4. Откройте браузер: http://localhost:5000

## Возможности

- 📝 Простой интерфейс для ввода YouTube URL
- ⚙️ Настройка параметров (количество клипов, aspect ratio, качество)
- 🔄 Реал-тайм отслеживание прогресса через Server-Sent Events
- 🎬 Предпросмотр результатов с видео-превью (для API режима)
- 📊 Отображение viral score, hook sentence и причины выбора клипа
- 💾 Кнопки скачивания для каждого клипа

## Режимы работы

### API Mode (по умолчанию)
- Быстрая обработка через MuAPI
- Результаты в виде hosted URLs
- Встроенный видео-плеер

### Local Mode
- Полностью оффлайн обработка (кроме LLM вызова)
- Результаты сохраняются в `output/` директорию
- Поддержка локальных файлов

## Архитектура

```
├── app.py                    Flask backend
├── templates/
│   └── index.html           Главная страница
└── static/
    ├── style.css            Стили (light/dark mode)
    └── app.js               Frontend логика
```

## API Endpoints

- `GET /` - Главная страница
- `POST /api/generate` - Запуск генерации
- `GET /api/status/<job_id>` - Статус задачи
- `GET /api/progress/<job_id>` - SSE поток с прогрессом
