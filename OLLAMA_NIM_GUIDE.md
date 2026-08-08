# Использование Ollama и NVIDIA NIM

Теперь вы можете использовать **локальные модели через Ollama** или **NVIDIA NIM** для анализа highlights вместо OpenAI/Gemini.

## 🦙 Ollama (полностью локально)

### Установка Ollama

**Windows:**
```bash
# Скачайте с https://ollama.com/download
# Или через winget:
winget install Ollama.Ollama
```

**Linux/Mac:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### Запуск и установка модели

```bash
# Запустите сервер Ollama
ollama serve

# В другом терминале: скачайте модель
ollama pull llama3.1:8b

# Или другую модель:
ollama pull mistral
ollama pull qwen2.5:7b
ollama pull deepseek-r1:7b
```

### Настройка в .env

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_NUM_CTX=16384
```

**Важно:** `OLLAMA_NUM_CTX=16384` поднимает контекстное окно. По умолчанию Ollama использует 4096 токенов, что обрежет длинные транскрипты. 16K достаточно для 20-минутных видео.

### Запуск

```bash
python main.py "https://youtube.com/watch?v=..." --mode local
```

Или через веб-интерфейс: выберите "Local" режим.

---

## 🚀 NVIDIA NIM

NVIDIA NIM предоставляет OpenAI-совместимый API для моделей на GPU.

### Два варианта:

#### 1. Hosted Catalog (облако)

Бесплатный доступ к Meta Llama, Mistral, Nemotron и другим моделям через https://build.nvidia.com

```bash
# Получите ключ на https://build.nvidia.com
# Добавьте в .env:
LLM_PROVIDER=nim
NIM_API_KEY=nvapi-xxxxxxxxx
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_MODEL=meta/llama-3.1-8b-instruct
```

**Доступные модели:**
- `meta/llama-3.1-8b-instruct`
- `meta/llama-3.1-70b-instruct`
- `mistralai/mistral-7b-instruct-v0.3`
- `nvidia/nemotron-4-340b-instruct`
- И многие другие: [build.nvidia.com/explore](https://build.nvidia.com/explore)

#### 2. Self-Hosted (на своём GPU)

Запустите NIM контейнер на вашем сервере с GPU:

```bash
# Пример для Llama 3.1 8B
docker run -it --rm --gpus all \
  -p 8000:8000 \
  nvcr.io/nim/meta/llama-3.1-8b-instruct:latest
```

Затем в `.env`:
```bash
LLM_PROVIDER=nim
NIM_BASE_URL=http://localhost:8000/v1
NIM_API_KEY=placeholder  # не нужен для self-hosted
NIM_MODEL=meta/llama-3.1-8b-instruct
```

---

## 📊 Сравнение провайдеров

| Провайдер | Скорость | Качество | Стоимость | Требования |
|-----------|----------|----------|-----------|------------|
| **OpenAI** | ⚡⚡⚡ Быстро | 🌟🌟🌟 Отлично | 💰 ~$0.15/видео | API ключ |
| **Gemini** | ⚡⚡⚡ Быстро | 🌟🌟🌟 Отлично | 💰 ~$0.08/видео | API ключ |
| **Ollama** | ⚡ Средне | 🌟🌟 Хорошо | 🆓 Бесплатно | GPU/CPU, 8GB+ RAM |
| **NIM (hosted)** | ⚡⚡⚡ Быстро | 🌟🌟🌟 Отлично | 🆓 Бесплатно (лимиты) | API ключ |
| **NIM (self-hosted)** | ⚡⚡ Быстро | 🌟🌟🌟 Отлично | 🆓 Бесплатно | NVIDIA GPU, 24GB+ VRAM |

---

## 🎯 Рекомендации

**Для экспериментов:** Ollama + `llama3.1:8b` — полностью локально, без API ключей

**Для продакшна:** OpenAI `gpt-4o-mini` или Gemini `gemini-2.5-flash` — надёжно и быстро

**Для GPU сервера:** NIM self-hosted — максимальная скорость + полный контроль

**Бесплатно + качество:** NVIDIA NIM hosted catalog

---

## 🔧 Troubleshooting

### Ollama: "Could not reach Ollama"
```bash
# Убедитесь, что сервер запущен:
ollama serve
```

### Ollama: "no model named llama3.1:8b"
```bash
# Скачайте модель:
ollama pull llama3.1:8b
```

### NIM: "API key not set"
```bash
# Для hosted: получите ключ на https://build.nvidia.com
# Для self-hosted: укажите любой placeholder
NIM_API_KEY=placeholder
```

### Модель медленно работает
- **Ollama:** Уменьшите модель (попробуйте `llama3.1:8b` вместо `:70b`)
- **Ollama:** Используйте GPU если доступна (автоматически)
- Увеличьте `LOCAL_LLM_TIMEOUT` если нужно больше времени

---

## 📚 Ссылки

- **Ollama:** [ollama.com](https://ollama.com) · [Docs](https://github.com/ollama/ollama/blob/main/docs/api.md)
- **NVIDIA NIM:** [build.nvidia.com](https://build.nvidia.com) · [Docs](https://docs.nvidia.com/nim)
- **OpenAI-compatible API:** [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility)
