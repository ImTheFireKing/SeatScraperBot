# Use an official Python image as base
FROM python:3.11-slim

# Set environment variables to avoid prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && apt-get install -y \
    wget unzip curl \
    libnss3 libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 libxfixes3 \
    libxi6 libxrandr2 libxss1 libxtst6 libglib2.0-0 \
    fonts-liberation libasound2 libatk-bridge2.0-0 libgtk-3-0 \
    libgbm1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Download and install Chrome for Testing
RUN mkdir -p /opt/chrome
RUN curl -Lo /opt/chrome/chrome-linux.zip https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.165/linux64/chrome-linux64.zip \
    && unzip /opt/chrome/chrome-linux.zip -d /opt/chrome \
    && ln -s /opt/chrome/chrome-linux64/chrome /usr/bin/chrome \
    && rm /opt/chrome/chrome-linux.zip

# Download and install matching ChromeDriver
RUN mkdir -p /opt/chromedriver
RUN curl -Lo /opt/chromedriver/chromedriver-linux64.zip https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.165/linux64/chromedriver-linux64.zip \
    && unzip /opt/chromedriver/chromedriver-linux64.zip -d /opt/chromedriver \
    && ln -s /opt/chromedriver/chromedriver-linux64/chromedriver /usr/bin/chromedriver \
    && rm /opt/chromedriver/chromedriver-linux64.zip

# Copy application files
WORKDIR /app
COPY . .

# Install Python dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev gcc \
    && apt-get clean && rm -rf /var/lib/apt/lists/*


RUN pip install --no-cache-dir -r requirements.txt

# Expose ports if needed
EXPOSE 8080

# Command to run the script
CMD ["python", "class_finder.py"]