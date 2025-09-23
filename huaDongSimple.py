import time
from selenium import webdriver
from selenium.webdriver.common.by import By

# 创建一个浏览器对象
driver = webdriver.Chrome()

# 发送请求
driver.get('https://www.helloweba.net/demo/2017/unlock/')

# 1.定位滑动按钮
short_obj = driver.find_element(By.XPATH, '//div[@class="bar1 bar"]/div[@class="slide-to-unlock-handle"]')

# 2.按住
# 创建一个动作链对象，参数就是浏览器对象
action_obj = webdriver.ActionChains(driver)

# 点击并且按住，参数就是定位的按钮
action_obj.click_and_hold(short_obj)

# 定位整条滑块
long_obj = driver.find_element(By.XPATH, '//div[@class="bar1 bar"]/div[@class="slide-to-unlock-bg"]')

# 得到它的宽高
size = long_obj.size
width = size['width']

# 定位滑动坐标，其中width表示移动的宽度，0表示移动的高度，这里制作水平移动，不做垂直移动。
action_obj.move_by_offset(width, 0).perform()

# 4.松开滑动
action_obj.release()
time.sleep(3)
