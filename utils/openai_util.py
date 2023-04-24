import openai; openai.api_key = open('open_ai.key', 'r').read()

def chat_complete(messages, max_tokens=1024, temperature=0.5, debug=False):
    stop = False
    res = ''
    if debug:
        for m in messages:
            print(m["role"],'\t',"-"*30)
            print(m["content"])
            print()
    total_tokens = 0
    last_token = 0
    while not stop:
        success = False
        while not success:
            try:
                response = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo-0301", messages=messages,
                    temperature=temperature, max_tokens=max_tokens
                )
                success = True
                res += response.choices[0].message.content
                total_tokens += response.usage.total_tokens
                last_token = response.usage.total_tokens
                # if debug:
                #     print(json.dumps(response.usage),response.choices[0].message.content.replace('\n',' '))
                if response.choices[0].finish_reason == 'stop':
                    stop = True
                else:
                    messages.append({"role": 'user', "content": response.choices[0].message.content})
            except Exception as e:
                print(e)
                # time.sleep(5)
    if debug:
        print('assistant','\t',"-"*30)
        print(res, '\n')
        print('usage','\t',"-"*30)
        print('last:', last_token, 'total:', total_tokens)
    return res

def translate(text,debug=False):
    messages = [
        {'role': 'system', 'content': "あなたの役割は論文を翻訳することです。これから論文の一部を送信するので、翻訳を行ってください。"},
        {'role': 'user', 'content': text},
    ]
    res = chat_complete(messages,max_tokens=4000, temperature=0., debug=debug)
    return(res)