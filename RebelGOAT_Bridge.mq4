//+------------------------------------------------------------------+
//|                                             RebelGOAT_Bridge.mq4 |
//|                                                    Rebel Funding |
//+------------------------------------------------------------------+
#property copyright "Rebel Funding"
#property link      ""
#property version   "1.00"
#property strict

extern string CommandFileName = "commands.csv";
extern string StateFileName = "account_state.csv";
extern int TimerInterval = 1; // Seconds

string lastProcessedCommandId = "";

int OnInit() {
   EventSetTimer(TimerInterval);
   Print("RebelGOAT Bridge EA Started.");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {
   EventKillTimer();
   Print("RebelGOAT Bridge EA Stopped.");
}

void OnTimer() {
   WriteAccountState();
   ReadAndExecuteCommands();
}

void WriteAccountState() {
   int handle = FileOpen(StateFileName, FILE_WRITE|FILE_CSV|FILE_ANSI, ",");
   if(handle == INVALID_HANDLE) return;
   
   // Header
   FileWrite(handle, "Balance", "Equity", "Margin", "FreeMargin");
   // Data
   FileWrite(handle, AccountBalance(), AccountEquity(), AccountMargin(), AccountFreeMargin());
   
   FileWrite(handle, "---POSITIONS---");
   FileWrite(handle, "Ticket", "Symbol", "Type", "Lots", "OpenPrice", "CurrentPrice", "Profit");
   
   for(int i=0; i<OrdersTotal(); i++) {
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) {
         FileWrite(handle, OrderTicket(), OrderSymbol(), OrderType(), OrderLots(), OrderOpenPrice(), OrderClosePrice(), OrderProfit());
      }
   }
   
   FileClose(handle);
}

void ReadAndExecuteCommands() {
   int handle = FileOpen(CommandFileName, FILE_READ|FILE_CSV|FILE_ANSI, ",");
   if(handle == INVALID_HANDLE) return;
   
   string cmdId = FileReadString(handle);
   string action = FileReadString(handle);
   string symbol = FileReadString(handle);
   string direction = FileReadString(handle);
   double lots = StringToDouble(FileReadString(handle));
   
   FileClose(handle);
   
   // If no command, or we've already processed this command, exit
   if(cmdId == "" || cmdId == lastProcessedCommandId || action == "ACK") return;
   
   // New command!
   Print("Received Command: ", cmdId, " ", action, " ", symbol, " ", direction, " ", lots);
   
   if(action == "OPEN") {
      int type = (direction == "BUY") ? OP_BUY : OP_SELL;
      double price = (type == OP_BUY) ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);
      int ticket = OrderSend(symbol, type, lots, price, 3, 0, 0, "RebelGOAT", 0, 0, (type==OP_BUY)?Blue:Red);
      if(ticket < 0) {
         Print("OrderSend failed with error #", GetLastError());
      } else {
         Print("Trade opened successfully! Ticket: ", ticket);
      }
   }
   else if(action == "CLOSE") {
      int expectedType = (direction == "BUY") ? OP_BUY : OP_SELL;
      for(int i=OrdersTotal()-1; i>=0; i--) {
         if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) {
            if(OrderSymbol() == symbol && OrderType() == expectedType) {
               double closePrice = (OrderType() == OP_BUY) ? MarketInfo(symbol, MODE_BID) : MarketInfo(symbol, MODE_ASK);
               bool res = OrderClose(OrderTicket(), OrderLots(), closePrice, 3, White);
               if(!res) {
                   Print("OrderClose failed with error #", GetLastError());
               } else {
                   Print("Trade closed successfully! Ticket: ", OrderTicket());
               }
            }
         }
      }
   }
   
   lastProcessedCommandId = cmdId;
   
   // Acknowledge the command so Python knows it's done
   int h = FileOpen(CommandFileName, FILE_WRITE|FILE_CSV|FILE_ANSI, ",");
   if(h != INVALID_HANDLE) {
      FileWrite(h, cmdId, "ACK", symbol, direction, lots);
      FileClose(h);
   }
}
