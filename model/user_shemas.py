from __future__ import annotations

from typing import Optional

from bson import ObjectId
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)

    model_config = ConfigDict(extra="ignore")

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class NewUser(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    is_active: bool = False

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class User(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    username: str
    email: str = Field(min_length=5, max_length=254)
    is_active: bool
    bio: Optional[str] = Field(default=None, max_length=280)
    profile_image_url: Optional[str] = None
    photo_profile: Optional[str] = None


class CurrentUser(User):
    id: Optional[str] = Field(default=None, alias="_id")
    password: str
    avatar_key: Optional[str] = None
    model_config = ConfigDict(populate_by_name=True)

    @field_validator("id", mode="before")
    @classmethod
    def convert_id(cls, v):
        return str(v) if isinstance(v, ObjectId) else v


class UserPublic(User):
    id: Optional[str] = Field(default=None, alias="_id")
    model_config = ConfigDict(populate_by_name=True)

    @field_validator("id", mode="before")
    @classmethod
    def convert_id(cls, v):
        return str(v) if isinstance(v, ObjectId) else v


class UserInDB(UserPublic):
    pass


class UserProfile(UserPublic):
    followers: int
    following: int
    post_num: int
    is_following: bool


class UserInfo(UserPublic):
    followers: Optional[list[str]] = None
    following: Optional[list[str]] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    username: Optional[str] = Field(default=None, min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    bio: Optional[str] = Field(default=None, max_length=280)


class UserSearch(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class VerificationRequiredResponse(UserPublic):
    message: str = "Verification required"
    verification_required: bool = True
    verification_token: str
    expires_in: int
    token_type: str = "bearer"


class InactiveLoginResponse(BaseModel):
    detail: str = "Account verification required"
    code: str = "ACCOUNT_NOT_VERIFIED"
    verification_required: bool = True
    verification_token: str
    expires_in: int
    token_type: str = "bearer"


class ConfirmationSentResponse(BaseModel):
    message: str = "Confirmation code sent"
    retry_after: int


class RetryAfterErrorResponse(BaseModel):
    detail: str
    retry_after: int


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(
        min_length=8,
        max_length=128,
        validation_alias=AliasChoices("new_password", "newPassword", "password"),
    )


class ConfirmationRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")
