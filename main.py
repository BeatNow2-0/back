import asyncio
from fastapi import FastAPI
from pymongo.errors import PyMongoError
from config.changeStream import watch_changes
from routes.users_routes import router as users_router
from routes.posts_routes import router as posts_router
from routes.interactions_routes import router as interactions_router
from routes.follow_routes import router as follow_router
from routes.lyrics_routes import router as lyrics_routes
from routes.routes import router as routes_router
from routes.search_routes import router as search_router
from routes.filter_routes import router as filter_router
from routes.mail_routes import router as mail_router
from routes.download_routes import router as donwload_router
from prometheus_client import start_http_server
import uvicorn
from config.db import handle_database_error
from fastapi.middleware.cors import CORSMiddleware

# Iniciar la aplicación
app = FastAPI()

origins = [
    "https://app-web-mauve.vercel.app",  # frontend producción
    "http://localhost:3000",              # si tu front en dev corre aquí
    "http://127.0.0.1:3000",
    "http://localhost:5173",              # ajusta según tu entorno
]

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,      # lista explícita porque allow_credentials=True
    allow_credentials=True,     # si necesitas cookies / Authorization
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["set-cookie"],  # opcional
    max_age=600,
)

# Incluir los routers
app.include_router(users_router, prefix="/v1/api/users")
app.include_router(posts_router, prefix="/v1/api/posts")
app.include_router(interactions_router, prefix="/v1/api/interactions")
app.include_router(lyrics_routes, prefix="/v1/api/lyrics")
app.include_router(follow_router, prefix="/v1/api/follows")
app.include_router(search_router, prefix="/v1/api/search")
app.include_router(filter_router, prefix="/v1/api/filter")
app.include_router(mail_router, prefix="/v1/api/mail")
app.include_router(donwload_router, prefix="/v1/api/download")
app.include_router(routes_router)
# Manejador de excepciones global para errores de base de datos
app.add_exception_handler(PyMongoError, handle_database_error)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(watch_changes())

def main():
    # Iniciar el servidor de Prometheus
    start_http_server(8000)
    # Iniciar el servidor de FastAPI
    uvicorn.run(app, host="localhost", port=8001)

if __name__ == "__main__":
    # Iniciar la aplicación
    main()
