from datetime import timedelta
from fastapi import HTTPException, Depends, APIRouter
from fastapi.security import OAuth2PasswordRequestForm
import bcrypt
from model.user_shemas import NewUser
from config.db import users_collection
from config.security import guardar_log, ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token

router = APIRouter()

@router.post("/token")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user_dict = await users_collection.find_one({"username": form_data.username})
    
    if not user_dict:
        guardar_log("Token failed - Incorrect username: " + form_data.username)
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Convertir hash a bytes si viene como string
    hashed_password = user_dict.get("password")
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode("utf-8")

    # Verificar contraseña
    if not bcrypt.checkpw(form_data.password.encode("utf-8"), hashed_password):
        guardar_log("Token failed - Incorrect password for username: " + form_data.username)
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Si todo bien → generar token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_dict["username"]},
        expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer"}
