# NUDEX Library Service

Microservicio para gestión de favoritos, historial y playlists de usuarios.

## 🚀 Stack

- **FastAPI** + **Python 3.11**
- **MongoDB** - Base de datos de documentos
- **Pydantic** - Validación de datos
- **RabbitMQ** - Eventos

## 📊 Entidades

- **Favorites**: Usuario + lista de videos favoritos
- **History**: Historial de reproducción
- **Playlists**: Playlists personalizadas

## 📡 Endpoints

```
GET  /health                    # Health check
GET  /favorites                 # Favoritos del usuario
POST /favorites                 # Agregar/quitar favorito
GET  /history                   # Historial de reproducción
POST /history                   # Agregar al historial
GET  /playlists                 # Playlists del usuario
POST /playlists                 # Crear playlist
PUT  /playlists/{id}           # Actualizar playlist
DELETE /playlists/{id}         # Eliminar playlist
```

## 🔧 Features

- ✅ CRUD completo de favoritos
- ✅ Historial de reproducción
- ✅ Gestión de playlists
- ✅ Autenticación por header x-user-id
- ✅ Validación Pydantic
- ✅ Eventos RabbitMQ
- ✅ Cache MongoDB
