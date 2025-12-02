from datetime import timedelta
import asyncio
import os
from typing import Annotated, List
import shutil
from bson import ObjectId
from requests import post
from passlib.context import CryptContext
from bson import Binary
from model.post_shemas import PostInDB
from model.user_shemas import NewUser, User, UserInDB, UserProfile
from model.lyrics_shemas import Lyrics, LyricsInDB
from config.security import (
    get_current_user_without_confirmation,
    get_lyric_id,
    get_post_id_saved,
    get_user,
    get_username,
    guardar_log,
    SSH_USERNAME_RES,
    SSH_PASSWORD_RES,
    SSH_HOST_RES,
    get_current_user,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    get_user_id,
)
from config.db import parse_list, users_collection, interactions_collection, get_database, lyrics_collection, follows_collection, post_collection
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import File, HTTPException, Depends, UploadFile, status, APIRouter
import paramiko
from routes.follow_routes import count_followers, count_following
import pymongo

from routes.mail_routes import send_confirmation

# Iniciar router
router = APIRouter()

# passlib context: produce and verify bcrypt hashes as strings
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# Helpers
def hash_password(password: str) -> str:
    """Hash a plain password and return the hash as a str."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against stored hash (both as str)."""
    return pwd_context.verify(plain_password, hashed_password)


# Registro
@router.post("/register")
async def register(user: NewUser):
    # 1) comprobaciones iniciales
    existing_user = await users_collection.find_one({"username": user.username})
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    existing_user = await users_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    # 2) hash de la contraseña y creación preliminar en BD (almacenamos siempre str)
    password_hash = hash_password(user.password)
    user_dict = user.dict()
    user_dict["password"] = password_hash

    result = await users_collection.insert_one(user_dict)

    ssh = None
    try:
        # 3) Conexión SSH y creación de carpetas remotas
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=SSH_HOST_RES,
            username=SSH_USERNAME_RES,
            password=SSH_PASSWORD_RES,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )

        user_id = await get_user_id(user_dict["username"])  # se espera string
        remote_base = f"/var/www/html/beatnow/{user_id}"
        mkdir_cmd = f"sudo -n mkdir -p {remote_base}/photo_profile {remote_base}/posts"
        copy_remote_default = f"sudo -n cp /var/www/html/res/photo-profile.jpg {remote_base}/photo_profile/photo_profile.png"

        # Ejecutar mkdir
        stdin, stdout, stderr = ssh.exec_command(mkdir_cmd, timeout=20)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()
        if rc != 0:
            raise RuntimeError(f"Remote mkdir failed: rc={rc} out='{out}' err='{err}'")

        # Intentar copiar fichero remoto por defecto
        stdin, stdout, stderr = ssh.exec_command(copy_remote_default, timeout=20)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()

        if rc != 0:
            # si falta el fichero remoto, subimos uno local por SFTP
            if "No such file" in err or "cannot stat" in err:
                sftp = ssh.open_sftp()
                try:
                    local_default = "/opt/beatnow-back/static/photo-profile.jpg"
                    if not os.path.exists(local_default):
                        raise RuntimeError("Local default profile image missing for upload")
                    remote_target = f"{remote_base}/photo_profile/photo_profile.png"
                    sftp.put(local_default, remote_target)
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass
            else:
                raise RuntimeError(f"Remote copy failed: rc={rc} out='{out}' err='{err}'")

        # Enviar comprobación de correo en background (no bloquear)
        try:
            # build the user object to send to mailer
            created_user = await get_user(await get_username(str(result.inserted_id)))
            # schedule background task
            asyncio.create_task(send_confirmation(created_user))
        except Exception:
            # no bloquear si falla el envío
            pass

    except Exception as e:
        # 4) rollback: borrar usuario creado en BD si hubo fallo en la parte remota
        try:
            await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
        except Exception:
            pass
        # Propagar error con detalle
        raise HTTPException(status_code=500, detail=f"Remote setup unexpected error: {str(e)}")
    finally:
        # 5) cerrar ssh si existe
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass

    # 6) si todo OK, devolver id del usuario creado (fuera del finally)
    return {"_id": str(result.inserted_id)}


@router.delete("/delete")
async def delete_user(current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Obtener el ID del usuario
    user_id = await get_user_id(current_user.username)
    if not user_id:
        raise HTTPException(status_code=500, detail="User ID not found")
    try:
        # Conexión SSH
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=SSH_HOST_RES, username=SSH_USERNAME_RES, password=SSH_PASSWORD_RES)

            # Eliminar la carpeta del usuario en el servidor
            user_dir = f"/var/www/html/beatnow/{user_id}"
            ssh.exec_command(f"sudo rm -rf {user_dir}")

            # Verificar si la carpeta se borró correctamente
            _, stderr, _ = ssh.exec_command(f"test -d {user_dir}")
            if stderr.channel.recv_exit_status() == 0:
                raise HTTPException(status_code=500, detail="Error deleting user directory from server")

        # Obtener los IDs de los posts del usuario
        user_posts = await post_collection.find({"user_id": ObjectId(user_id)}, {"_id": 1}).to_list(None)
        post_ids = [post["_id"] for post in user_posts]
        # Eliminar los follows
        await follows_collection.delete_many({"user_id_following": user_id})
        await follows_collection.delete_many({"user_id_followed": user_id})
        # Eliminar las letras
        await lyrics_collection.delete_many({"user_id": user_id})
        # Eliminar todas las interacciones asociadas a los posts del usuario
        await interactions_collection.delete_many({"post_id": {"$in": post_ids}})
        await interactions_collection.delete_many({"user_id": user_id})
        # Eliminar todos los posts del usuario
        await post_collection.delete_many({"user_id": user_id})
        # Eliminar al usuario de la colección de usuarios
        delete_result = await users_collection.delete_one({"_id": ObjectId(user_id)})
        if delete_result.deleted_count == 1:
            print("User deleted successfully from database")
        else:
            print("User deletion failed or user not found in database")

        return {"message": "User deleted successfully"}

    except paramiko.AuthenticationException as e:
        raise HTTPException(status_code=500, detail="SSH authentication failed")
    except paramiko.SSHException as e:
        raise HTTPException(status_code=500, detail="SSH connection error")
    except paramiko.ssh_exception.NoValidConnectionsError as e:
        raise HTTPException(status_code=500, detail="No valid SSH connections available")
    except pymongo.errors.PyMongoError as e:
        raise HTTPException(status_code=500, detail="Database error: " + str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="An error occurred: " + str(e))


# Recoger datos del usuario actual
@router.get("/users/me")
async def read_users_me(
    current_user: Annotated[NewUser, Depends(get_current_user_without_confirmation)],
):
    user_id = await get_user_id(current_user.username)
    return {**current_user.dict(), "id": str(user_id)}


@router.post("/login")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user_dict = await users_collection.find_one({"username": form_data.username})
    if not user_dict:
        guardar_log("Login failed - Incorrect username: " + form_data.username)
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Verify password using passlib (stored hash is a str)
    if not verify_password(form_data.password, user_dict.get('password', '')):
        guardar_log("Login failed - Incorrect password for username: " + form_data.username)
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Create access token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_dict["username"]}, expires_delta=access_token_expires
    )
    guardar_log("Login successful for username: " + form_data.username)
    # You may wish to return the token and token_type to follow OAuth2 spec
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/saved-posts")
async def get_saved_posts(current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    user_id = await get_user_id(current_user.username)
    saved_posts = await interactions_collection.find({"user_id": user_id, "saved_date": {"$exists": True}}).to_list(None)

    # Convertir ObjectId a cadenas
    for post in saved_posts:
        post["_id"] = str(post["_id"])
        post["creator_id"] = await get_post_id_saved(post["post_id"])

    return {"saved_posts": saved_posts}


@router.get("/liked-posts")
async def get_liked_posts(current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    user_id = await get_user_id(current_user.username)
    liked_posts = await interactions_collection.find({"user_id": user_id, "like_date": {"$exists": True}}).to_list(None)

    # Convertir ObjectId a cadenas
    for post in liked_posts:
        post["_id"] = str(post["_id"])

    return {"liked_posts": liked_posts}


@router.get("/lyrics", response_model=List[LyricsInDB])
async def get_user_lyrics(current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    user_id = await get_user_id(current_user.username)
    user_lyrics = await lyrics_collection.find({"user_id": user_id}).to_list(None)

    # Convertir ObjectId a cadenas
    for lyric in user_lyrics:
        lyric["_id"] = str(lyric["_id"])

    return user_lyrics


@router.get("/posts/{username}", response_model=List[PostInDB])
async def list_user_publications(username: str, current_user: User = Depends(get_current_user), db=Depends(get_database)):
    # Verificar si el usuario solicitado existe
    user_exists = await users_collection.find_one({"username": username})
    if not user_exists:
        raise HTTPException(status_code=404, detail="User not found")

    # Buscar todas las publicaciones del usuario
    user_id = str(user_exists["_id"])
    user_publications = post_collection.find({"user_id": user_id})
    if not user_publications:
        return []
    results = []
    async for document in user_publications:  # Asynchronous iteration
        document["_id"] = str(document["_id"])  # Convert ObjectId to string
        results.append(document)
    return results


@router.get("/profile/{user_id}", response_model=UserProfile)
async def get_user_profile(user_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    user_dict = await users_collection.find_one({"_id": ObjectId(user_id)})
    if user_dict:
        userindb = User(**user_dict)
        user_id_following = await get_user_id(current_user.username)
        if user_id_following == user_id:
            isFollowing = True
        else:
            existing_follow = await follows_collection.find_one({"user_id_followed": user_id, "user_id_following": user_id_following})
            isFollowing = bool(existing_follow)

        _followers = await count_followers(user_id) or 0
        _following = await count_following(user_id) or 0
        post_count = await post_collection.count_documents({"user_id": user_id}) or 0

        profile = UserProfile(
            **userindb.dict(),
            _id=str(user_id),
            followers=_followers["followers_count"],
            following=_following["following_count"],
            post_num=post_count,
            is_following=isFollowing,
        )

        return profile.dict()
    else:
        raise HTTPException(status_code=404, detail="User id not Found")


@router.delete("/delete_photo_profile")
async def delete_photo_profile(current_user: NewUser = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = await get_user_id(current_user.username)
    user_photo_dir = f"/var/www/html/beatnow/{user_id}/photo_profile"
    default_photo_path = "/var/www/html/res/photo-profile.jpg"

    try:
        # Establish SSH connection and perform the operation
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=SSH_HOST_RES, username=SSH_USERNAME_RES, password=SSH_PASSWORD_RES)

            # Command to replace user's photo profile with default
            command = f"sudo cp {default_photo_path} {user_photo_dir}/photo_profile.png"
            stdin, stdout, stderr = ssh.exec_command(command)

            # Wait for the command to finish and capture any errors
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_message = stderr.read().decode()
                raise HTTPException(status_code=500, detail=f"Command failed: {error_message}")

    except paramiko.SSHException as e:
        raise HTTPException(status_code=500, detail=f"SSH error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

    return {"message": "Photo profile deleted successfully"}


@router.put("/change_photo_profile")
async def change_photo_profile(
    file: UploadFile = File(...),
    current_user: NewUser = Depends(get_current_user)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = await get_user_id(current_user.username)
    if not user_id:
        raise HTTPException(status_code=404, detail="User id not found")

    user_photo_dir = f"/var/www/html/beatnow/{user_id}/photo_profile"
    remote_filename = "photo_profile.png"
    remote_path = os.path.join(user_photo_dir, remote_filename)

    try:
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=SSH_HOST_RES, username=SSH_USERNAME_RES, password=SSH_PASSWORD_RES, timeout=20)

            # --- Use SFTP for directory creation / cleaning / upload ---
            sftp = ssh.open_sftp()

            # create dirs if not exists (walk parents)
            try:
                # try to stat user_photo_dir; if fails, create
                sftp.stat(user_photo_dir)
            except IOError:
                # create intermediate dirs
                parent = f"/var/www/html/beatnow/{user_id}"
                try:
                    sftp.mkdir(parent)
                except IOError:
                    # ignore if exists
                    pass
                try:
                    sftp.mkdir(user_photo_dir)
                except IOError:
                    # ignore if exists
                    pass

            # Clear existing files inside the photo_profile directory safely
            try:
                for name in sftp.listdir(user_photo_dir):
                    path_to_remove = user_photo_dir + "/" + name
                    try:
                        sftp.remove(path_to_remove)
                    except IOError:
                        # if it's a directory, attempt rmdir (shouldn't be)
                        try:
                            sftp.rmdir(path_to_remove)
                        except Exception:
                            # ignore individual remove errors but log
                            pass
            except IOError:
                # Directory might still be missing or unreadable; attempt to create it
                try:
                    sftp.mkdir(user_photo_dir)
                except Exception:
                    pass

            # Upload new file
            # ensure we start from beginning of file
            await file.seek(0)
            # read content into memory (ok for profile pics)
            content = await file.read()
            import io
            file_obj = io.BytesIO(content)
            file_obj.seek(0)

            with sftp.open(remote_path, "wb") as dst:
                shutil.copyfileobj(file_obj, dst)

            # Set file permissions (rw-r--r--)
            try:
                sftp.chmod(remote_path, 0o644)
            except Exception:
                pass

            # Ensure directory permissions are ok (rwxr-xr-x)
            try:
                sftp.chmod(user_photo_dir, 0o755)
            except Exception:
                pass

            sftp.close()

    except paramiko.AuthenticationException:
        raise HTTPException(status_code=500, detail="SSH authentication failed")
    except paramiko.SSHException as e:
        raise HTTPException(status_code=500, detail=f"SSH error: {str(e)}")
    except Exception as e:
        # bubble up filesystem permission issue or others
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

    return {
        "message": "Photo profile updated successfully",
        "path": f"https://51.91.109.185/beatnow/{user_id}/photo_profile/{remote_filename}"
    }


@router.put("/update")
async def update_user(user_update: UserInDB, current_user: NewUser = Depends(get_current_user), db = Depends(get_database)):
    current_userdict = await users_collection.find_one({"username": current_user.username})
    if current_userdict["_id"] != ObjectId(user_update.id):
        raise HTTPException(status_code=400, detail="You can only update your own user data")

    # Hash the password before saving it (store as str)
    password_hash = hash_password(user_update.password)
    user = User(**user_update.dict())
    user_dict = user.dict()
    user_dict['password'] = password_hash
    if not user_dict.get('is_active'):
        user_dict['is_active'] = True

    # Encuentra y actualiza el usuario en la base de datos
    result = await users_collection.update_one(
        {"_id": ObjectId(user_update.id)},
        {"$set": {k: v for k, v in user_dict.items() if v is not None}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    elif result.modified_count == 0:
        raise HTTPException(status_code=400, detail="No new data to update")

    return {"message": "User updated successfully"}


@router.get("/check-username")
async def check_username(username: str):
    # Check if the username is already taken
    existing_user = await users_collection.find_one({"username": username})
    if existing_user:
        return {"status": "ko", "detail": "Username already registered"}
    return {"status": "ok", "detail": "Username is available"}


@router.get("/check-email")
async def check_email(email: str):
    # Check if the username is already taken
    existing_user = await users_collection.find_one({"email": email})
    if existing_user:
        return {"status": "ko", "detail": "Email already registered"}
    return {"status": "ok", "detail": "Email is available"}

@router.put("/change_password")
async def change_password(
    current_password: str,
    new_password: str,
    current_user: NewUser = Depends(get_current_user)
):
    # 1) Obtener usuario desde BD
    user_dict = await users_collection.find_one({"username": current_user.username})
    if not user_dict:
        raise HTTPException(status_code=404, detail="User not found")

    # 2) Verificar contraseña actual
    stored_hash = user_dict["password"]
    if not verify_password(current_password, stored_hash):
        raise HTTPException(status_code=400, detail="Incorrect current password")

    # 3) Hashear nueva contraseña
    new_hash = hash_password(new_password)

    # 4) Guardar en BD
    result = await users_collection.update_one(
        {"_id": ObjectId(user_dict["_id"])},
        {"$set": {"password": new_hash}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to update password")

    return {"message": "Password updated successfully"}
