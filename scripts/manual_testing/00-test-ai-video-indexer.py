import requests
import time
# Configuration
API_URL = "https://api.videoindexer.ai"
API_KEY = "YOUR_API_KEY"
ACCOUNT_ID = "YOUR_ACCOUNT_ID"
LOCATION = "YOUR_LOCATION"
VIDEO_URL = "https://example.com/sample-video.mp4"
# Step 1: Get Access Token
def get_access_token():
   headers = {"Ocp-Apim-Subscription-Key": API_KEY}
   response = requests.get(f"{API_URL}/auth/{LOCATION}/Accounts/{ACCOUNT_ID}/AccessToken", headers=headers)
   return response.text.strip('"')
# Step 2: Upload Video
def upload_video(access_token):
   headers = {"Authorization": f"Bearer {access_token}"}
   params = {
       "name": "SampleVideo",
       "videoUrl": VIDEO_URL,
       "privacy": "Private",
   }
   response = requests.post(f"{API_URL}/{LOCATION}/Accounts/{ACCOUNT_ID}/Videos", headers=headers, params=params)
   return response.json()["id"]
# Step 3: Check Processing Status
def check_video_status(access_token, video_id):
   while True:
       headers = {"Authorization": f"Bearer {access_token}"}
       response = requests.get(f"{API_URL}/{LOCATION}/Accounts/{ACCOUNT_ID}/Videos/{video_id}/Index", headers=headers)
       status = response.json()["state"]
       if status == "Processed":
           return response.json()
       elif status == "Failed":
           raise Exception("Video processing failed.")
       time.sleep(10)
# Main Execution
access_token = get_access_token()
video_id = upload_video(access_token)
video_insights = check_video_status(access_token, video_id)
print("Video Insights:", video_insights)