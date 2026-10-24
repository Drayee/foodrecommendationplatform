from django.http import JsonResponse  # 将数据以JSON格式发给前端
from langchain.agents import create_agent  # 创建智能体的库
from langgraph.checkpoint.memory import InMemorySaver  # 让大模型拥有记忆功能(内存记忆)
from ..llm import ChatModel  # .表示当前文件
from ..tools.tools import send_email, sql_query
from django.shortcuts import render
from pydantic import BaseModel, Field
import  redis

redis_conn = redis.Redis(host="localhost", port=6379, db=0)  # redis默认16个数据库




# 初始化记忆功能
memory = InMemorySaver()

class EmailResp(BaseModel):
    code: str = Field(..., description = "状态码")
    data: str = Field(..., description = "验证码")
    msg: str = Field(..., description = "提示信息")

# 写一个方法，用来供前端URL地址访问
def send_code(email):  # request: 表示前端发送到后端的数据（请求方式、数据、类型）
    # 1. 初始化大模型
    llm = ChatModel.load_model_once()
    # 2. 提示词功能（第一个作用：设计大模型角色信息； 第二个作用：约束大模型，告诉大模型它需要做怎么/怎么做）
    prompt = """
        一、你是一个验证码发送、登录助手，你有2个工具：
            1: sql_query：执行SQL语句查询数据库
            2: send_email：执行邮件发送
        二、工具流程：必须严格按照以下流程进行工作
            步骤一：根据用户输入的信息（邮箱地址），判断是否是合法邮箱地址，构造相应的查询条件。
            步骤二：调用 sql_query 工具，查询 users 表中是否存在该用户信息及其邮箱。
            步骤三：如果用户存在且邮箱不为空，那么就必须先生成随机6位数的验证码。
            步骤四：将获取到的验证码作为参数，调用 send_email 工具发送邮件。
        三、反馈信息规范
            1. 如果 sql_query 查询结果为空，请返回：{"code": 400, "msg": "邮箱未注册！", "data":"null"}
            2. 如果 send_email 发送成功，请返回：{"code": 200, "msg": "验证码发送成功！", "data":"生成的验证码"}
            3. 如果 send_email 发送失败，请返回：{"code": 500, "msg": "邮件发送失败！", "data":"null"}
        三：邮件格式，随机生成好看的样式发送
    """

    # 3. 创建大模型智能体
    agent = create_agent(
        model=llm,  # model参数：表示使用的是哪个大模型
        system_prompt=prompt,  # system_prompt参数：提示词
        tools=[send_email, sql_query],  # tools参数：大模型需要调用工具，使用@tools定义的方法
        checkpointer=memory,
        response_format= EmailResp,

    )
    # 4. 调用智能体
    result = agent.invoke(
        {"messages": [{"role": "user", "content": email}]},
        {"configurable": {"thread_id": email}}  # sessionId用户标识：隔离用户对话
    )

    print(result["structured_response"].model_dump())
    ans = result["structured_response"].model_dump()  # 表示获取类中对应的字典{code: 200, msg:"验证码发送成功"， data:909982}
    if ans["code"] == "200":
        redis_conn.set(f"login:code:{email}", ans["data"], ex=300)  # redis中，存储格式以 key:value 的格式进行存储
        return JsonResponse(ans)
    else:
        return JsonResponse({"code":500, "msg":"服务器错误，请稍后重试！"})


def login_code(account, code):   # acount: 邮箱   code: 验证码
    key = f"login:code:{account}"
    code_bytes = redis_conn.get(key) # 通过get方法获取数据库键值对的值：get(key), 存在判断是否与用户输入的验证码相同
    if code_bytes and code_bytes.decode() == code:
        return JsonResponse({"code":200, "msg":"正在登陆！"})
    else:
        return JsonResponse({"code":500, "msg":"验证码过期或错误！"})



