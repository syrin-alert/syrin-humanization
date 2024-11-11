import os
import pika
import json
import logging
import requests

# Configure INFO level logging
logging.basicConfig(level=logging.INFO)

# Disable pika debug logs, setting them to WARNING or higher
logging.getLogger("pika").setLevel(logging.WARNING)

# Load RabbitMQ settings from environment variables
rabbitmq_host = os.getenv('RABBITMQ_HOST', '')
rabbitmq_port = int(os.getenv('RABBITMQ_PORT', 5672))
rabbitmq_vhost = os.getenv('RABBITMQ_VHOST', '')
rabbitmq_user = os.getenv('RABBITMQ_USER', '')
rabbitmq_pass = os.getenv('RABBITMQ_PASS', '')
rabbitmq_ttl_dlx = int(os.getenv('RABBITMQ_TTL_DLX', 60000))  # 60 seconds TTL (60000 ms)

# Load Ollama AI settings
OLLAMA_HOSTNAME = os.getenv('OLLAMA_HOSTNAME', '')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3.2:3b')

# Load Ollama AI prompts
PROMPT_AUDIO = os.getenv('PROMPT_AUDIO', """
                        Write a technical alert notification with a title mentioning the environment name. Include the environment name, the identified problem in the error, and a summary of the situation. The environment name is represented by the content inside the brackets ([]).

                        Response structure:

                            [Title mentioning: Alert, the environment [environment name] is no longer stable.]
                            Problem: [problem identified in the error]
                            Hint of the possible cause of the error: [give a very brief hint of the possible cause of the error]
                            Situation summary: [please state possible impacts that the error may cause in the environment; this information should be very brief]
                            Error message follows: [ error message ]

                        Mood scale: 3 (light)
                        Remember that the environment name is only the content inside the brackets! Error: 
                        """)
PROMPT_MESSAGE = os.getenv('PROMPT_MESSAGE', """
                        Write a technical alert notification with a title. Include the environment name, the identified problem in the error, and a summary of the situation. The environment name is represented by the content inside the brackets ([]).

                        Response structure:

                            [Title: Alert, the environment [environment name] shows instability.]
                            Problem: [problem identified in the error]
                            Hint of the possible cause of the error: [give a brief hint of the possible cause of the error]
                            Situation summary: [please state possible impacts that the error may cause in the environment; this information should be very brief]
                            How it can be solved: [please provide a way to solve the error]
                            Error message follows: [ error message ]

                        Mood scale: 2 (light)
                        Remember that the environment name is only the content inside the brackets! Error:                         
                        """)


def connect_to_rabbitmq():
    try:
        credentials = pika.PlainCredentials(rabbitmq_user, rabbitmq_pass)
        client_properties = {"connection_name": "Syrin Text Humanized Agent"}
        parameters = pika.ConnectionParameters(
            host=rabbitmq_host,
            port=rabbitmq_port,
            virtual_host=rabbitmq_vhost,
            credentials=credentials,
            client_properties=client_properties
        )
        return pika.BlockingConnection(parameters)
    except Exception as e:
        logging.error(f"Error connecting to RabbitMQ: {str(e)}")
        return None

def requestOllama(text, prompt):
    url = f"http://{OLLAMA_HOSTNAME}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "prompt": f"{prompt} {text}"
    }
    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        response_data = response.json()
        return response_data.get("response")
    except requests.RequestException as e:
        logging.error(f"Error in request to Ollama: {str(e)}")
        return None  # Returns None in case of error

def declare_reprocess_queue(channel, reprocess_queue_name, dead_letter_queue_name):
    try:
        channel.queue_declare(
            queue=reprocess_queue_name,
            durable=True,
            arguments={
                'x-message-ttl': rabbitmq_ttl_dlx,
                'x-dead-letter-exchange': '',
                'x-dead-letter-routing-key': dead_letter_queue_name
            }
        )
        logging.info(f"Queue '{reprocess_queue_name}' created or confirmed with TTL of {rabbitmq_ttl_dlx} ms and DLX '{dead_letter_queue_name}'.")
    except Exception as e:
        logging.error(f"Error declaring the reprocessing queue '{reprocess_queue_name}': {str(e)}")

def declare_standard_queue(channel, queue_name):
    try:
        channel.queue_declare(queue=queue_name, durable=True)
        logging.info(f"Queue '{queue_name}' checked or created.")
    except Exception as e:
        logging.error(f"Error declaring queue '{queue_name}': {str(e)}")

def reprocess_message(channel, message, reprocess_queue_name):
    try:
        channel.basic_publish(
            exchange='',
            routing_key=reprocess_queue_name,
            body=json.dumps(message, ensure_ascii=False),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        logging.info(f"Message sent to reprocess queue '{reprocess_queue_name}': {message['text']}")
    except Exception as e:
        logging.error(f"Error reprocessing the message to '{reprocess_queue_name}': {str(e)}")

def send_to_humanized_queue(channel, text_humanized, original_message, queue_name):
    try:
        message = {
            'original_text': original_message['text'],
            'level': original_message.get('level', ''),
            'humanized_text': text_humanized
        }
        channel.basic_publish(
            exchange='',
            routing_key=queue_name,
            body=json.dumps(message, ensure_ascii=False),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        logging.info(f"Humanized message sent to '{queue_name}' \o/")
        # logging.info(f"Humanized message sent to '{queue_name}' queue: {message}")
    except Exception as e:
        logging.error(f"Error sending message to the humanized queue '{queue_name}': {str(e)}")

def on_message_callback(channel, method_frame, header_frame, body):
    try:
        message = json.loads(body.decode())
        queue_name = method_frame.routing_key

        if queue_name == '01_syrin_notification_message_process':
            prompt = PROMPT_MESSAGE
            output_queue = '02_syrin_notification_message_process_humanized'
            reprocess_queue = '01_syrin_notification_message_reprocess'
        elif queue_name == '01_syrin_notification_audio_process':
            prompt = PROMPT_AUDIO
            output_queue = '02_syrin_notification_audio_process_humanized'
            reprocess_queue = '01_syrin_notification_audio_reprocess'
        else:
            logging.error(f"Unknown queue: {queue_name}")
            channel.basic_ack(method_frame.delivery_tag)
            return

        logging.info(f"Processing new message from {queue_name}...")
        # logging.info(f"Processing message from {queue_name}: {message['text']}")
        text_humanized = requestOllama(message['text'], prompt)
        
        if text_humanized:
            send_to_humanized_queue(channel, text_humanized, message, output_queue)
        else:
            logging.error(f"Failed to humanize the message: {message['text']}")
            reprocess_message(channel, message, reprocess_queue)
        
        channel.basic_ack(method_frame.delivery_tag)
    except Exception as e:
        logging.error(f"Error in callback processing message: {str(e)}")
        channel.basic_ack(method_frame.delivery_tag)

def consume_messages():
    connection = connect_to_rabbitmq()
    if connection is None:
        logging.error("Connection to RabbitMQ failed. Exiting the application.")
        return

    try:
        channel = connection.channel()
        
        declare_standard_queue(channel, '01_syrin_notification_audio_process')
        declare_standard_queue(channel, '01_syrin_notification_message_process')
        declare_standard_queue(channel, '02_syrin_notification_audio_process_humanized')
        declare_standard_queue(channel, '02_syrin_notification_message_process_humanized')

        declare_reprocess_queue(channel, '01_syrin_notification_message_reprocess', '01_syrin_notification_message_process')
        declare_reprocess_queue(channel, '01_syrin_notification_audio_reprocess', '01_syrin_notification_audio_process')

        channel.basic_consume(queue='01_syrin_notification_message_process', on_message_callback=on_message_callback)
        channel.basic_consume(queue='01_syrin_notification_audio_process', on_message_callback=on_message_callback)

        logging.info("Waiting for messages...")
        channel.start_consuming()
    except Exception as e:
        logging.error(f"Error in message consumption: {str(e)}")
    finally:
        if connection and connection.is_open:
            connection.close()
            logging.info("Connection to RabbitMQ closed.")

if __name__ == "__main__":
    try:
        logging.info("Syrin text humanized - started \o/")
        consume_messages()
    except Exception as e:
        logging.error(f"Error running the application: {str(e)}")
