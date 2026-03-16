import pandas as pd
import numpy as np
import csv
from decimal import Decimal
from datetime import datetime, timedelta
import time

def initialize(context):
    # 初始化策略
    g.security = "600570.SS"
    set_universe(g.security)

def handle_data(context, data):
    pass
    
def before_trading_start(context, data):
    log.info("盘前处理开始")
    g.current_date = context.blotter.current_dt.strftime("%Y%m%d")
    g.previous_date = context.previous_date.strftime("%Y%m%d")
    g.stock_list = get_Ashares(g.current_date) # 获取回测当天A股列表
    
    #获取为ST的股票
    g.st_status = get_stock_status(g.stock_list,'ST',query_date=g.current_date)
    #获取当日停牌的股票
    g.halt_status = get_stock_status(g.stock_list,'HALT',query_date=g.current_date)
    #获取当日退市的股票
    g.delisting_status = get_stock_status(g.stock_list,'DELISTING',query_date=g.current_date)
    #过滤ST、停牌和退市的股票
    for stock in g.stock_list.copy():
        if g.halt_status[stock] or g.delisting_status[stock] or g.st_status[stock]:
            g.stock_list.remove(stock)
    
    #读取相应文件    
    path1=get_research_path()+'TD/'+"test.csv"
    path2=get_research_path()+'TD/'+"finish.csv"
    path3=get_research_path()+'TD/'+"cancel.csv"
    TD_df=pd.read_csv(path1)
    finish_df = pd.read_csv(path2)
    g.use_column=TD_df.columns
    cancel_df=pd.read_csv(path3)
    
    history=get_history(31, frequency='1d', field=[ 'close', 'high', 'low'], security_list=g.stock_list, fq='pre', include=True)
    g.close_df_dict = {stock: group['close'].values for stock, group in history.groupby('code')}
    g.low_df_dict = {stock: group['low'].values for stock, group in history.groupby('code')}
    g.high_df_dict = {stock: group['high'].values for stock, group in history.groupby('code')}
    
    new_df,finish_df,cancel_df=process(context,history,TD_df,finish_df,cancel_df)
    new_df.to_csv(path1,index=False)
    finish_df.to_csv(path2,index=False)
    cancel_df.to_csv(path3,index=False)
    
def process(context,history,TD_df,finish_df,cancel_df):
    new_df = pd.DataFrame(columns=g.use_column,dtype="str")
    
    for stock in g.stock_list.copy():
        filtered_df = TD_df[TD_df.iloc[:, 0] == stock]
        if not filtered_df.empty:
            status = check_status(context,stock,df,filtered_df.iloc[0,3]) #检查计数状态
            filtered_df.iloc[0,20] = status #更新计数状态
            if status == 1: #正常
                if g.close_df_dict[stock][-1] < g.low_df_dict[stock][-3]:
                    count = filtered_df.iloc[0,18]+1
                    filtered_df.iloc[0,18] = count
                    if count <= 8:
                        filtered_df.iloc[0,count+3] = g.previous_date
                        if count == 8:
                            filtered_df.iloc[0,12] = g.close_df_dict[stock][-1]
                        new_df=pd.concat([new_df,filtered_df],ignore_index=True)
                    elif count < 13:                                              
                        filtered_df.iloc[0,count+4] = g.previous_date
                        new_df=pd.concat([new_df,filtered_df],ignore_index=True)
                    else:#计数到达13时
                        filtered_df.iloc[0,count+4] = g.previous_date
                        #判断是否完美
                        if g.low_df_dict[stock][-1] >= filtered_df.iloc[0,12]:
                            filtered_df.iloc[0,19]=1
                        else:
                            filtered_df.iloc[0,19]=0
                        #todo:计算止损点
                        stoppoint=get_stoppoint(filtered_df)
                        filtered_df.iloc[0,24]=stoppoint
                        
                        #将此条记录放入finish文件，代表计数完成
                        finish_df=pd.concat([finish_df,filtered_df],ignore_index=True)
                else:
                    continue
                     
            elif status==2:
                filtered_df.iloc[0,4:19]=0
                new_df=pd.concat([new_df,filtered_df],ignore_index=True)
            elif status==3:
                log.info("出现相反setup")
                log.info(stock)
                filtered_df.iloc[0,20]=3
                cancel_df=pd.concat([cancel_df,filtered_df],ignore_index=True)
                # new_record=build_new_setup(df,stock)
                # new_df=pd.concat([new_df,new_record],ignore_index=True)
            else :
                log.info("出现相同setup")
                log.info(stock)
                filtered_df.iloc[0,20]=4
                cancel_df=pd.concat([cancel_df,filtered_df],ignore_index=True)
                new_record=build_new_setup(df,stock)
                new_df=pd.concat([new_df,new_record],ignore_index=True)
                
        else:
            if check_setup_buy(df)==True:
                new_record=build_new_setup(df,stock)
                new_df=pd.concat([new_df,new_record],ignore_index=True)
                
    return new_df,finish_df,cancel_df