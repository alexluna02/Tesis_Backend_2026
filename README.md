---
title: AgroScan API
emoji: 🌿
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# ProyectoTesis - Detección de Enfermedades en Maíz

API con FastAPI para segmentación de enfermedades en hojas de maíz usando DeepLabV3+.

## 📋 Descripción

El proyecto incluye:
- **API Backend**: FastAPI con endpoints para predicción de segmentación y explicabilidad (XAI).
- **Modelo**: DeepLabV3+ con encoder MiT-B1, entrenado para 4 clases: Fondo/Sana, Tizón, Roya, Mancha_Blanca.
- **Front-end**: React con Vite para subir imágenes y visualizar resultados.

## 🚀 Ejecutar el Proyecto

### 1. Preparar Entorno

```powershell
# Instalar Python y Node.js (si no los tienes)
winget install Python.Python.3.11
winget install OpenJS.NodeJS

# Crear entorno virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Instalar dependencias Python
pip install -r requirements.txt

# Instalar dependencias Node.js
cd FrontReactIA
npm install
cd ..
```

### 2. Entrenar el Modelo (opcional, si tienes el dataset)

Coloca tu dataset COCO en `./dataset-maiz/` con estructura:
```
dataset-maiz/
├── train/
│   ├── imagenes...
│   └── _annotations.coco.json
└── valid/
    ├── imagenes...
    └── _annotations.coco.json
```

Ejecuta el entrenamiento:
```powershell
python train_maize.py
```

El mejor modelo se guarda como `best_model.pth`. Renómbralo a `modelofinal_bigdata.pth` o actualiza la variable `MODEL_PATH`.

### 3. Configurar PostgreSQL y ejecutar API

La API usa PostgreSQL y por defecto se conecta a la base de datos `bd_tesis`.

Variables de entorno opcionales:
- `DATABASE_NAME` (por defecto `bd_tesis`)
- `DATABASE_USER` (por defecto `postgres`)
- `DATABASE_PASSWORD` (por defecto `postgres`)
- `DATABASE_HOST` (por defecto `localhost`)
- `DATABASE_PORT` (por defecto `5432`)
- `DATABASE_URL` (si prefieres usar una URL completa)
- `MODEL_PATH` (ruta del modelo, por defecto `best_model.pth`)
- `JWT_SECRET_KEY` (clave secreta para firmar tokens)

# (Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) ; (& d:\ProyectoTesis\.venv\Scripts\Activate.ps1) 
```powershell
# En terminal 1: API
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# En terminal 2: Front-end
cd FrontReactIA
npm run dev
```

Accede a:
- **API Docs**: http://localhost:8000/docs
- **Front-end**: http://localhost:5173

## 📊 Endpoints API

- `GET /`: Información general
- `GET /health`: Estado del servidor y conexión a la base de datos
- `POST /auth/register`: Registro de usuario
- `POST /auth/login`: Inicio de sesión y obtención de access + refresh token
- `POST /auth/refresh`: Renovación del token de acceso
- `POST /auth/logout`: Cierra sesión y revoca refresh token
- `GET /users/me`: Información del usuario autenticado
- `GET /enfermedades`: Catálogo de enfermedades
- `POST /analisis`: Registra un análisis realizado por el usuario
- `GET /analisis`: Lista los análisis del usuario autenticado
- `POST /predict/segmentation`: Segmentación de imagen
- `POST /predict/xai`: Explicabilidad con Grad-CAM

```powershell
uvicorn main:app --reload
```

4. Abre tu navegador en: `http://127.0.0.1:8000/docs` para ver la documentación interactiva (Swagger).

---

## 🧩 Endpoints disponibles

- `GET /` - Mensaje de bienvenida
- `GET /health` - Verifica que el servidor esté activo y si el modelo está cargado
- `POST /predict/segmentation` - Realiza inferencia de segmentación sobre una imagen

---

## 🧠 Uso del modelo entrenado

1. Asegúrate de tener el archivo de pesos entrenados en la raíz del proyecto con el nombre:

   - `modelofinal_bigdata.pth`

   También puedes usar otra ruta configurando la variable de entorno `MODEL_PATH` antes de iniciar el servidor.

2. Inicia el servidor:

```powershell
uvicorn main:app --reload
```

3. Prueba la inferencia usando `curl` (o desde tu aplicación frontend):

```powershell
curl -X POST "http://127.0.0.1:8000/predict/segmentation" -F "file=@ruta/a/imagen.jpg" -H "accept: application/json"
```

El endpoint devolverá JSON con las clases y la máscara segmentada codificada en base64 (`mask_base64_png`).

> Tip: Para mostrar la máscara en un navegador, puedes usar el mismo string base64 dentro de un `<img src="data:image/png;base64,{mask_base64_png}">`.

---

## 🎨 Frontend React

También incluye un frontend React en la carpeta `FrontReactIA/`:

1. Ve a la carpeta del frontend:
   ```bash
   cd FrontReactIA
   ```

2. Instala dependencias:
   ```bash
   npm install
   ```

3. Ejecuta el frontend:
   ```bash
   npm run dev
   ```

4. Abre `http://localhost:5173` en tu navegador.

---

> Nota: La API ahora persiste usuarios, refresh tokens y análisis en PostgreSQL mediante `bd_tesis`.
