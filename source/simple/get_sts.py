import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import json
from datetime import datetime

def get_session_token_basic():
    """
    基本的なget-session-tokenの使用例
    IAMユーザーの認証情報から一時認証情報を取得
    """
    try:
        # STSクライアントを作成
        sts_client = boto3.client('sts')
        
        # 一時認証情報を取得（デフォルト12時間、最大36時間）
        response = sts_client.get_session_token(
            DurationSeconds=3600  # 1時間（3600秒）
        )
        
        credentials = response['Credentials']
        
        print("=== 一時認証情報 ===")
        print(f"AccessKeyId: {credentials['AccessKeyId']}")
        print(f"SecretAccessKey: {credentials['SecretAccessKey']}")
        print(f"SessionToken: {credentials['SessionToken'][:50]}...")
        print(f"Expiration: {credentials['Expiration']}")
        
        return credentials
        
    except NoCredentialsError:
        print("エラー: AWS認証情報が設定されていません")
        return None
    except ClientError as e:
        print(f"AWS APIエラー: {e}")
        return None

def get_session_token_with_mfa(mfa_device_arn, token_code):
    """
    MFA（多要素認証）を使用した一時認証情報の取得
    """
    try:
        sts_client = boto3.client('sts')
        
        response = sts_client.get_session_token(
            DurationSeconds=3600,
            SerialNumber=mfa_device_arn,  # MFAデバイスのARN
            TokenCode=token_code          # MFAトークンコード
        )
        
        credentials = response['Credentials']
        
        print("=== MFA認証による一時認証情報 ===")
        print(f"AccessKeyId: {credentials['AccessKeyId']}")
        print(f"SecretAccessKey: {credentials['SecretAccessKey']}")
        print(f"SessionToken: {credentials['SessionToken'][:50]}...")
        print(f"Expiration: {credentials['Expiration']}")
        
        return credentials
        
    except ClientError as e:
        print(f"MFA認証エラー: {e}")
        return None

def assume_role_example(role_arn, session_name):
    """
    AssumeRoleを使用して他のロールの一時認証情報を取得
    """
    try:
        sts_client = boto3.client('sts')
        
        response = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=3600
        )
        
        credentials = response['Credentials']
        
        print("=== AssumeRoleによる一時認証情報 ===")
        print(f"AccessKeyId: {credentials['AccessKeyId']}")
        print(f"SecretAccessKey: {credentials['SecretAccessKey']}")
        print(f"SessionToken: {credentials['SessionToken'][:50]}...")
        print(f"Expiration: {credentials['Expiration']}")
        print(f"AssumedRoleUser: {response['AssumedRoleUser']}")
        
        return credentials
        
    except ClientError as e:
        print(f"AssumeRoleエラー: {e}")
        return None

def use_temporary_credentials(credentials):
    """
    取得した一時認証情報を使用してAWSサービスにアクセスする例
    """
    try:
        # 一時認証情報を使用してS3クライアントを作成
        s3_client = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # S3バケット一覧を取得（権限があれば）
        response = s3_client.list_buckets()
        print("=== S3バケット一覧 ===")
        for bucket in response['Buckets']:
            print(f"- {bucket['Name']}")
            
    except ClientError as e:
        print(f"S3アクセスエラー: {e}")

def save_credentials_to_file(credentials, filename="temp_credentials.json"):
    """
    一時認証情報をファイルに保存
    """
    try:
        # datetime型をISO形式の文字列に変換
        cred_data = {
            'AccessKeyId': credentials['AccessKeyId'],
            'SecretAccessKey': credentials['SecretAccessKey'],
            'SessionToken': credentials['SessionToken'],
            'Expiration': credentials['Expiration'].isoformat()
        }
        
        with open(filename, 'w') as f:
            json.dump(cred_data, f, indent=2)
        
        print(f"認証情報を {filename} に保存しました")
        
    except Exception as e:
        print(f"ファイル保存エラー: {e}")

def get_caller_identity():
    """
    現在の認証情報の情報を確認
    """
    try:
        sts_client = boto3.client('sts')
        response = sts_client.get_caller_identity()
        
        print("=== 現在の認証情報 ===")
        print(f"UserId: {response.get('UserId')}")
        print(f"Account: {response.get('Account')}")
        print(f"Arn: {response.get('Arn')}")
        
        return response
        
    except ClientError as e:
        print(f"認証情報取得エラー: {e}")
        return None

# メイン実行部分
if __name__ == "__main__":
    print("AWS STS一時認証情報取得のサンプル")
    print("=" * 50)
    
    # 現在の認証情報を確認
    get_caller_identity()
    print()
    
    # 基本的な一時認証情報の取得
    temp_credentials = get_session_token_basic()
    
    if temp_credentials:
        print()
        # 取得した一時認証情報でAWSサービスにアクセス
        use_temporary_credentials(temp_credentials)
        print()
        
        # 認証情報をファイルに保存
        save_credentials_to_file(temp_credentials)
    
    # MFA使用例（実際のMFAデバイスARNとトークンが必要）
    print("\n" + "=" * 50)
    print("MFA使用例（コメントアウト）:")
    print("# mfa_arn = 'arn:aws:iam::123456789012:mfa/username'")
    print("# token = '123456'  # MFAアプリからの6桁コード")
    print("# get_session_token_with_mfa(mfa_arn, token)")
    
    # AssumeRole使用例（実際のロールARNが必要）
    print("\nAssumeRole使用例（コメントアウト）:")
    print("# role_arn = 'arn:aws:iam::123456789012:role/MyRole'")
    print("# assume_role_example(role_arn, 'MySession')")