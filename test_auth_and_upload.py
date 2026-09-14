"""
Verification Test Script for Auth & Document Upload Module
"""

import sys
import auth

def test_auth():
    print("=== Testing Password Hashing & JWT Tokens ===")
    
    password = "MySecurePassword123!"
    hashed = auth.hash_password(password)
    print(f"[+] Hashed Password: {hashed[:30]}...")
    
    # Verify correct password
    assert auth.verify_password(password, hashed) is True, "Password verification failed for correct password!"
    print("[+] Correct password verified successfully.")
    
    # Verify incorrect password
    assert auth.verify_password("WrongPassword", hashed) is False, "Password verification passed for WRONG password!"
    print("[+] Incorrect password rejected successfully.")
    
    # Create & decode JWT token
    token = auth.create_access_token({"sub": 42, "email": "test@example.com"})
    print(f"[+] Generated Access Token: {token[:30]}...")
    
    payload = auth.decode_access_token(token)
    assert payload is not None, "Failed to decode JWT token!"
    assert str(payload.get("sub")) == "42", "Token payload sub mismatch!"
    assert payload.get("email") == "test@example.com", "Token payload email mismatch!"
    print("[+] JWT Token created & decoded successfully!")
    print("\n✓ ALL AUTH UNIT TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_auth()
