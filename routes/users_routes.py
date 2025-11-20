from datetime import timedelta
import asyncio
import os
from typing import Annotated, List
import shutil
from bson import ObjectId
from passlib.handlers.bcrypt import bcrypt
import bcrypt
from requests import post
from model.post_shemas import PostInDB
from model.user_shemas import NewUser, User, UserInDB, UserProfile
from model.lyrics_shemas import Lyrics, LyricsInDB
from config.security import  get_current_user_without_confirmation, get_lyric_id, get_post_id_saved, get_user, get_username, guardar_log, SSH_USERNAME_RES, SSH_PASSWORD_RES, SSH_HOST_RES, \
    get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token, get_user_id
from config.db import parse_list, users_collection, interactions_collection, get_database, lyrics_collection, follows_collection, post_collection
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import File, HTTPException, Depends, UploadFile, status, APIRouter
import paramiko
from routes.follow_routes import count_followers, count_following
import pymongo

from routes.mail_routes import send_confirmation

# Iniciar router
router = APIRouter()

# Registro
@router.post("/register")
async def register(user: NewUser):
    # Check if the username is already taken
    existing_user = await users_collection.find_one({"username": user.username})
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    existing_user = await users_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    # Hash the password before saving it
    password_hash = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt())
    user_dict = user.dict()
    user_dict['password'] = password_hash
    result = await users_collection.insert_one(user_dict)
    ssh = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=SSH_HOST_RES,
                    username=SSH_USERNAME_RES,
                    password=SSH_PASSWORD_RES,
                    timeout=15, banner_timeout=15, auth_timeout=15)

        user_id = await get_user_id(user_dict['username'])
        remote_base = f"/var/www/html/beatnow/{user_id}"
        mkdir_cmd = f"sudo -n mkdir -p {remote_base}/photo_profile {remote_base}/posts"
        copy_remote_default = f"sudo -n cp /var/www/html/res/photo-profile.jpg {remote_base}/photo_profile/photo_profile.png"

        # Ejecuta mkdir y verifica
        stdin, stdout, stderr = ssh.exec_command(mkdir_cmd, timeout=20)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()
        if rc != 0:
            await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
            raise HTTPException(status_code=500, detail=f"Remote mkdir failed: rc={rc} out='{out}' err='{err}'")

        # Intenta copiar el default remoto; si falla por falta de fichero, sube localmente por SFTP
        stdin, stdout, stderr = ssh.exec_command(copy_remote_default, timeout=15)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()

        if rc != 0:
            # si no existe /var/www/html/res/photo-profile.jpg en remoto, hacemos SFTP upload
            # Comprueba el tipo de error: si stderr contiene 'No such file' entonces subimos
            if "No such file" in err or "cannot stat" in err:
                sftp = ssh.open_sftp()
                try:
                    # ruta local del fichero por defecto en tu proyecto (crea este fichero si no existe)
                    local_default = "/opt/beatnow-back/static/photo-profile.jpg"
                    if not os.path.exists(local_default):
                        # si no existe local, no bloqueamos: limpiamos y fallamos con detalle
                        await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
                        raise HTTPException(status_code=500, detail="Local default profile image missing for upload")
                    remote_target = f"{remote_base}/photo_profile/photo_profile.png"
                    sftp.put(local_default, remote_target)
                finally:
                    try:
                        sftp.close()
                    except:
                        pass
            else:
                # otro error en copy
                await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
                raise HTTPException(status_code=500, detail=f"Remote copy failed: rc={rc} out='{out}' err='{err}'")

        # send confirmation in background (non-blocking)
        try:
            asyncio.create_task(send_confirmation(await get_user(await get_username(result.inserted_id))))
        except Exception:
            # no bloquear si el envio falla; loggear si tienes logger
            pass

    except paramiko.AuthenticationException:
        await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
        raise HTTPException(status_code=500, detail="SSH authentication failed")
    except paramiko.ssh_exception.NoValidConnectionsError:
        await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
        raise HTTPException(status_code=500, detail="No valid SSH connections available")
    except paramiko.SSHException as e:
        await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
        raise HTTPException(status_code=500, detail=f"SSH error: {str(e)}")
    except Exception as e:
        await users_collection.delete_one({"_id": ObjectId(result.inserted_id)})
        raise HTTPException(status_code=500, detail=f"Remote setup unexpected error: {str(e)}")
    finally:
            # Si todo va bien devolvemos el id del usuario creado
        return {"_id": str(result.inserted_id)}
        if ssh:
            try:
                ssh.close()
            except:
                pass

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
        #ELiminar los follows
        await follows_collection.delete_many({"user_id_following": user_id})
        await follows_collection.delete_many({"user_id_followed": user_id})
        #Eliminar las letras
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
    user_id= await get_user_id(current_user.username)
    return {**current_user.dict(), "id": str(user_id)}


'''
#Listar todos los usuarios
@router.get("/users")
async def get_all_users(token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)  
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.username == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unauthorized",
        )

    all_users = await users_collection.find({}, {"username": 1, "_id": 0}).to_list(length=None)
    usernames = [user["username"] for user in all_users]
    return {"usernames": usernames}
'''
@router.post("/login")  # Additional route
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user_dict = await users_collection.find_one({"username": form_data.username})
    if not user_dict:
        guardar_log("Login failed - Incorrect username: " + form_data.username)
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    user = NewUser(**user_dict)
    if not bcrypt.checkpw(form_data.password.encode('utf-8'), user_dict['password']):
        guardar_log("Login failed - Incorrect password for username: " + form_data.username)
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    if not user:
        raise HTTPException(
            status_code=400,
            detail="Incorrect username or password"
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    guardar_log("Login successful for username: " + form_data.username)
    return {"message": "ok"}

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

@router.get("/posts/{username}",response_model=List[PostInDB])
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
        if user_id_following==user_id:
            isFollowing = True
        else:
            existing_follow = await follows_collection.find_one({"user_id_followed": user_id, "user_id_following": user_id_following})
            if existing_follow:
                isFollowing = True
            else:
                isFollowing = False
        _followers = await count_followers(user_id)
        if _followers==None:
            _followers = 0
        _following = await count_following(user_id)
        if _following==None:
            _following = 0
        post_count = await post_collection.count_documents({"user_id": user_id})
        if post_count==None:
            post_count = 0
        #results = await list_user_publications(userindb.username)
        

        profile = UserProfile(
            **userindb.dict(),
            _id=str(user_id),
            followers=_followers["followers_count"],
            following=_following["following_count"],
            post_num=post_count,
            is_following=isFollowing,
            #publications=results  # Asegúrate de que 'publications' es un campo en tu modelo UserProfile
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
async def change_photo_profile(file: UploadFile = File(...), current_user: NewUser = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = await get_user_id(current_user.username)
    user_photo_dir = f"/var/www/html/beatnow/{user_id}/photo_profile"
    
    try:
        # Establish SSH connection
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=SSH_HOST_RES, username=SSH_USERNAME_RES, password=SSH_PASSWORD_RES)

            # Verify if the user's directory exists, if not, create it
            mkdir_command = f"test -d {user_photo_dir} || sudo mkdir -p {user_photo_dir}"
            stdin, stdout, stderr = ssh.exec_command(mkdir_command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_message = stderr.read().decode()
                raise HTTPException(status_code=500, detail=f"Failed to create directory: {error_message}")
            
            # Ensure the correct permissions
            chown_command = f"sudo chown -R $USER:$USER {user_photo_dir}"
            stdin, stdout, stderr = ssh.exec_command(chown_command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_message = stderr.read().decode()
                raise HTTPException(status_code=500, detail=f"Failed to set permissions: {error_message}")

            # Remove existing content in the user's profile photo folder
            rm_command = f"sudo rm -rf {user_photo_dir}/*"
            stdin, stdout, stderr = ssh.exec_command(rm_command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_message = stderr.read().decode()
                raise HTTPException(status_code=500, detail=f"Failed to remove existing files: {error_message}")

            # Save the new profile photo with a unique name and png format
            sftp = ssh.open_sftp()
            file_path = os.path.join(user_photo_dir, "photo_profile.png")
            with sftp.open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            sftp.close()

    except paramiko.SSHException as e:
        raise HTTPException(status_code=500, detail=f"SSH error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

    return {"message": "Photo profile updated successfully"}

@router.put("/update")
async def update_user(user_update: UserInDB, current_user: NewUser = Depends(get_current_user), db = Depends(get_database)):
    current_userdict = await users_collection.find_one({"username": current_user.username})
    if current_userdict["_id"]!=ObjectId(user_update.id):
        raise HTTPException(status_code=400, detail="You can only update your own user data")
    # Hash the password before saving it
    password_hash = bcrypt.hashpw(user_update.password.encode('utf-8'), bcrypt.gensalt())
    user = User(**user_update.dict())
    user_dict = user.dict()
    user_dict['password'] = password_hash
    if not user_dict['is_active']:
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