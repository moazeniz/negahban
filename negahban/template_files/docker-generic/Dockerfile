# Generic Docker template: replace the base image and the CMD with your own build/start commands.
# Liara routes traffic to port {{port}}; your process must listen on 0.0.0.0:{{port}}.
FROM {{base_image}}
WORKDIR /app
COPY . .
EXPOSE {{port}}
CMD ["python", "-m", "http.server", "{{port}}", "--bind", "0.0.0.0", "--directory", "public"]
