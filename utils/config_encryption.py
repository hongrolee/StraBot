"""
설정 파일 암호화 유틸리티
API 키, 시크릿 등 민감한 정보를 암호화하여 저장/로드
"""

import os
import logging
from pathlib import Path
from typing import Optional
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

logger = logging.getLogger(__name__)

# Windows 파일 보호용 (선택사항)
try:
    import win32security
    import ntsecuritycon
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False
    logger.warning("pywin32가 설치되지 않았습니다. Windows 파일 ACL 보호 기능을 사용할 수 없습니다.")


class ConfigEncryption:
    """설정 파일 암호화 관리 클래스"""
    
    # 암호화 대상 필드
    ENCRYPTED_FIELDS = {'api_key', 'api_secret', 'llm_api_key', 'bithumb_api_key', 'bithumb_api_secret'}
    
    def __init__(self, user_data_dir: Optional[Path] = None):
        """
        Args:
            user_data_dir: 사용자 데이터 디렉토리 (None이면 자동 감지)
        """
        if user_data_dir is None:
            user_data_dir = Path(os.getenv('LOCALAPPDATA', os.path.expanduser('~'))) / 'StraBot'
        
        self.user_data_dir = user_data_dir
        self.master_key_file = user_data_dir / '.master_key'
        self.secret_key_file = user_data_dir / '.secret_key'
        
        # 디렉토리 생성
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        
        # 마스터 키 로드 또는 생성
        self._load_or_create_master_key()
        
        # SECRET_KEY 생성 또는 로드
        self._load_or_create_secret_key()
    
    def _load_or_create_master_key(self):
        """마스터 키 로드 또는 생성"""
        if self.master_key_file.exists():
            try:
                self.key = self.master_key_file.read_bytes()
                self.cipher = Fernet(self.key)
                logger.info("마스터 키 로드 완료")
            except Exception as e:
                logger.error(f"마스터 키 로드 실패: {e}")
                self._create_master_key()
        else:
            self._create_master_key()
    
    def _create_master_key(self):
        """새 마스터 키 생성"""
        try:
            self.key = Fernet.generate_key()
            self.cipher = Fernet(self.key)
            self.master_key_file.write_bytes(self.key)
            
            # Windows 파일 보호
            if WIN32_AVAILABLE:
                self._protect_file_with_acl(self.master_key_file)
            
            # 파일 권한 제한 (Unix/Linux)
            if os.name != 'nt':
                os.chmod(self.master_key_file, 0o600)
            
            logger.info("마스터 키 생성 완료")
        except Exception as e:
            logger.error(f"마스터 키 생성 실패: {e}")
            raise
    
    def _load_or_create_secret_key(self):
        """Flask SECRET_KEY 로드 또는 생성"""
        if self.secret_key_file.exists():
            try:
                self.secret_key = self.secret_key_file.read_text(encoding='utf-8').strip()
                logger.info("SECRET_KEY 로드 완료")
            except Exception as e:
                logger.error(f"SECRET_KEY 로드 실패: {e}")
                self._create_secret_key()
        else:
            self._create_secret_key()
    
    def _create_secret_key(self):
        """새 Flask SECRET_KEY 생성"""
        try:
            # 32바이트 랜덤 키를 base64 인코딩하여 Flask SECRET_KEY로 사용
            import secrets
            self.secret_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8')
            self.secret_key_file.write_text(self.secret_key, encoding='utf-8')
            
            # Windows 파일 보호
            if WIN32_AVAILABLE:
                self._protect_file_with_acl(self.secret_key_file)
            
            # 파일 권한 제한 (Unix/Linux)
            if os.name != 'nt':
                os.chmod(self.secret_key_file, 0o600)
            
            logger.info("SECRET_KEY 생성 완료")
        except Exception as e:
            logger.error(f"SECRET_KEY 생성 실패: {e}")
            raise
    
    def _protect_file_with_acl(self, file_path: Path):
        """Windows 파일 ACL로 현재 사용자만 접근하도록 보호"""
        if not WIN32_AVAILABLE:
            return
        
        try:
            username = os.getenv('USERNAME') or os.getenv('USER')
            if not username:
                logger.warning("사용자명을 찾을 수 없어 파일 ACL을 설정할 수 없습니다.")
                return
            
            # 현재 사용자 SID 가져오기
            try:
                user_sid, domain, account_type = win32security.LookupAccountName(None, username)
            except Exception:
                # 도메인 사용자일 경우
                full_username = f"{os.getenv('USERDOMAIN', '')}\\{username}".strip('\\')
                user_sid, domain, account_type = win32security.LookupAccountName(None, full_username)
            
            # DACL 생성 (현재 사용자만 모든 권한)
            dacl = win32security.ACL()
            dacl.AddAccessAllowedAce(
                win32security.ACL_REVISION,
                ntsecuritycon.FILE_ALL_ACCESS,
                user_sid
            )
            
            # 파일에 DACL 적용
            win32security.SetFileSecurity(
                str(file_path),
                win32security.DACL_SECURITY_INFORMATION,
                dacl
            )
            
            logger.debug(f"파일 ACL 설정 완료: {file_path}")
        except Exception as e:
            logger.debug(f"파일 ACL 설정 실패 (선택사항이므로 계속 진행): {e}")
    
    def encrypt(self, value: str) -> str:
        """
        문자열 암호화
        
        Args:
            value: 암호화할 문자열
            
        Returns:
            암호화된 문자열 (base64 인코딩)
        """
        if not value:
            return value
        
        try:
            encrypted = self.cipher.encrypt(value.encode('utf-8'))
            return base64.urlsafe_b64encode(encrypted).decode('utf-8')
        except Exception as e:
            logger.error(f"암호화 실패: {e}")
            raise
    
    def decrypt(self, encrypted_value: str) -> str:
        """
        암호화된 문자열 복호화
        
        Args:
            encrypted_value: 암호화된 문자열 (base64 인코딩)
            
        Returns:
            복호화된 문자열
        """
        if not encrypted_value:
            return encrypted_value
        
        try:
            decoded = base64.urlsafe_b64decode(encrypted_value.encode('utf-8'))
            decrypted = self.cipher.decrypt(decoded)
            return decrypted.decode('utf-8')
        except Exception as e:
            # 마스터 키가 바뀌었거나 파일이 깨짐: 암호문을 키로 쓰면 거래소 인증만 조용히 실패하므로 빈 값으로 (2026-10-17)
            logger.error(f"복호화 실패: {e} — 저장된 API 키를 읽을 수 없습니다. 설정에서 API 키를 다시 넣어 주십시오")
            return ''
    
    def is_encrypted(self, value: str) -> bool:
        """
        값이 암호화되어 있는지 확인
        
        Args:
            value: 확인할 값
            
        Returns:
            암호화되어 있으면 True
        """
        if not value:
            return False
        
        try:
            # encrypt() 는 Fernet 토큰(늘 'gAAAAA' 로 시작)을 한 번 더 base64 로 싸므로, 한 겹 풀면 그 머리가 보여야 함
            # (전에는 '풀어서 32바이트 이상'이라 긴 평문 키(LLM 키 등)가 길이에 따라 '이미 암호화됨'으로 보여 평문으로 저장됐음, 2026-10-17)
            decoded = base64.urlsafe_b64decode(value.encode('utf-8'))
            return decoded.startswith(b'gAAAAA') and len(decoded) >= 57
        except Exception:
            return False
    
    def encrypt_config(self, config: dict) -> dict:
        """
        설정 딕셔너리의 민감한 필드 암호화
        
        Args:
            config: 설정 딕셔너리
            
        Returns:
            암호화된 설정 딕셔너리 (원본 수정 없이 새 딕셔너리 반환)
        """
        encrypted_config = config.copy()
        
        for field in self.ENCRYPTED_FIELDS:
            if field in encrypted_config and encrypted_config[field]:
                value = encrypted_config[field]
                # 이미 암호화되어 있지 않으면 암호화
                if not self.is_encrypted(str(value)):
                    try:
                        encrypted_config[field] = self.encrypt(str(value))
                        # 암호화 마킹 (선택사항)
                        encrypted_config[f"{field}_encrypted"] = True
                    except Exception as e:
                        logger.warning(f"필드 {field} 암호화 실패: {e}")
        
        return encrypted_config
    
    def decrypt_config(self, config: dict) -> dict:
        """
        설정 딕셔너리의 암호화된 필드 복호화
        
        Args:
            config: 설정 딕셔너리
            
        Returns:
            복호화된 설정 딕셔너리 (원본 수정 없이 새 딕셔너리 반환)
        """
        decrypted_config = config.copy()
        
        for field in self.ENCRYPTED_FIELDS:
            if field in decrypted_config and decrypted_config[field]:
                value = str(decrypted_config[field])
                # 암호화되어 있으면 복호화
                if self.is_encrypted(value):
                    try:
                        decrypted_config[field] = self.decrypt(value)
                        # 암호화 마킹 제거
                        decrypted_config.pop(f"{field}_encrypted", None)
                    except Exception as e:
                        logger.warning(f"필드 {field} 복호화 실패: {e}")
                # 평문이지만 _encrypted 마킹이 있으면 제거 (이상한 상태)
                elif decrypted_config.get(f"{field}_encrypted"):
                    decrypted_config.pop(f"{field}_encrypted", None)
        
        return decrypted_config
    
    @staticmethod
    def mask_sensitive_value(value: str, visible_chars: int = 4) -> str:
        """
        민감한 값 마스킹 (로그용)
        
        Args:
            value: 마스킹할 값
            visible_chars: 보여줄 문자 수 (앞뒤)
            
        Returns:
            마스킹된 문자열 (예: "abcd...xyz")
        """
        if not value or len(value) <= visible_chars * 2:
            return "***"
        
        return f"{value[:visible_chars]}...{value[-visible_chars:]}"


def sanitize_config_for_logging(config: dict) -> dict:
    """
    로깅용 설정 딕셔너리 생성 (민감 정보 마스킹)
    
    Args:
        config: 원본 설정 딕셔너리
        
    Returns:
        민감 정보가 마스킹된 설정 딕셔너리
    """
    sanitized = config.copy()
    # 마스킹만 하므로 ConfigEncryption() 을 만들지 않음 (만들면 기본 폴더에 키 파일을 새로 만들었음, 2026-10-17)
    
    for field in ConfigEncryption.ENCRYPTED_FIELDS:
        if field in sanitized and sanitized[field]:
            sanitized[field] = ConfigEncryption.mask_sensitive_value(str(sanitized[field]))
    
    return sanitized

