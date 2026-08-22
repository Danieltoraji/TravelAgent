import openai
client = openai.OpenAI(api_key="<$API_KEY>", 
base_url="https://$BASE_URL/v1/")

response = client.chat.completions.create(
    model="$模型ID",  # model to send to the proxy
    messages=[
        {
            "role": "user",
            "content": "Hello world"        
        }
    ]
)
print(response)