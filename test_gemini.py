import os
import sys

from google import genai

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"]
)

task = sys.argv[1]

with open(
    "prompts/system_prompt.txt",
    "r",
    encoding="utf-8"
) as f:
    prompt_template = f.read()

prompt = prompt_template.replace(
    "{TASK}",
    task
)

response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents=prompt
)

print(response.text)