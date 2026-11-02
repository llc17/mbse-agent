model ZLTECH_Full_Mission
  "全任务仿真模型：包含上楼(20级)、平台缓冲、下楼(20级)的全过程"
  
  // =============================================================
  // 1. 车辆物理参数 (保持不变，这是你的设计基准)
  // =============================================================
  parameter Real m = 85.0;         // 车重 85kg
  parameter Real g = 9.81;         // 重力加速度
  parameter Real I_pitch = 20.0;   // 俯仰转动惯量 (值越大，车身晃动越稳)

  // --- 轮组尺寸 ---
  parameter Real R_main = 0.127;   // 10寸主轮 (半径127mm)
  parameter Real R_nose = 0.07;    // 5.5寸鼻轮 (半径70mm)
  
  // --- 电机能力 ---
  parameter Real T_main_max = 60.0;// 主轮扭矩 60Nm (上楼主要动力)
  parameter Real T_nose_max = 25.0;// 鼻轮扭矩 25Nm (辅助过坎)
  
  // --- 关键几何尺寸 (核心设计) ---
  parameter Real wheelbase = 0.45; // 轴距 450mm
  parameter Real L_nose_ext = 0.32;// 鼻轮前伸 320mm (保证能探到下一级台阶)
  parameter Real h_nose = 0.19;    // 鼻轮轴高 190mm (大于台阶高150mm，形成"咬合"优势)

  parameter Real a = 0.22;         // 重心到前轴距离
  parameter Real b = 0.23;         // 重心到后轴距离 (重心略微靠后，利于上坡抓地)
  parameter Real L_nose_CoM = a + L_nose_ext; // 重心到鼻轮轴的水平距离

  // =============================================================
  // 2. 环境地形参数 (构造一座"人工山")
  // =============================================================
  parameter Real stepH = 0.15;     // 台阶高 15cm
  parameter Real stepD = 0.28;     // 台阶深 28cm
  parameter Integer n_steps = 20;  // 单侧台阶数量
  
  // --- 地形关键坐标点 (用于分段函数) ---
  // 1. 上楼起点 (X=2.0米处开始)
  parameter Real x_start_up = 2.0;                                
  // 2. 上楼终点 (楼梯总深 = 20 * 0.28)
  parameter Real x_end_up = x_start_up + n_steps * stepD;         
  // 3. 楼顶平台 (长度 2.0米，给车身姿态调整的时间)
  parameter Real len_flat = 2.0;                                  
  // 4. 下楼起点 (平台结束的位置)
  parameter Real x_start_down = x_end_up + len_flat;              
  // 5. 下楼终点
  parameter Real x_end_down = x_start_down + n_steps * stepD;     
  
  // 楼顶最高高度 (20级 * 0.15 = 3.0米)
  parameter Real H_peak = n_steps * stepH; 

  // --- 接触物理参数 (仿真优化的关键) ---
  // 注意：这里使用了"下楼优化版"的较软参数。
  // 理由：下楼是刚体撞击过程，如果刚度太硬(8e4)，计算机会算不过来导致卡死。
  // 虽然这会让上楼时轮胎看起来稍微瘪一点，但为了能跑通全过程，这是必要的工程妥协。
  parameter Real k_ground = 4.0e4;  // 地面刚度 (软化处理，像硬橡胶)
  parameter Real c_ground = 3.0e3;  // 地面阻尼 (减小阻尼，避免瞬时力突变)
  parameter Real mu_s = 0.85;       // 摩擦系数 (高抓地力)

  // =============================================================
  // 3. 状态变量
  // =============================================================
  Real x(start=0.0, fixed=true);    // 水平位置
  Real z(start=R_main + 0.01, fixed=true); // 垂直高度 (初始悬空1cm，防止嵌地)
  
  Real v(start=0.0, fixed=true);    // 速度 (从静止开始)
  Real vz(start=0, fixed=true);     // 垂直速度
  Real psi(start=0, fixed=true);    // 俯仰角 (Pitch)
  Real omega_psi(start=0, fixed=true); // 角速度

  // 各轮坐标与受力
  Real x_n, z_n, x_f, z_f, x_r, z_r; // 坐标
  Real h_n, h_f, h_r;                // 轮子正下方的地形高度
  Real Fz_n, Fx_n, Fz_f, Fx_f, Fz_r, Fx_r; // 支持力与牵引力
  
  // --- 智能控制变量 ---
  Real target_speed = 0.4; // 目标巡航速度 0.4 m/s
  Real power_factor;       // 动力因子 (自动油门/刹车)
  Boolean nose_on_ground;  // 鼻轮是否着地

equation
  // =============================================================
  // A. 几何运动学解算
  // =============================================================
  // 利用旋转矩阵，根据车身中心(x,z)和倾角(psi)计算三个轮子的位置
  x_f = x + a * cos(psi);
  z_f = z + a * sin(psi);
  
  x_r = x - b * cos(psi);
  z_r = z - b * sin(psi);
  
  // 鼻轮位置 (注意它比前轴高 h_nose-R_main)
  x_n = x + L_nose_CoM * cos(psi) - (h_nose - R_main) * sin(psi);
  z_n = z + L_nose_CoM * sin(psi) + (h_nose - R_main) * cos(psi);

// =============================================================
  // B. 全地形生成函数 (The Mountain Function)
  // =============================================================
  // 这是一个分段函数，根据轮子的 X 坐标决定它脚下的地面有多高。
  // 【关键技巧】使用了 noEvent() 包裹。
  // 作用：告诉仿真器，当轮子经过台阶棱角时，不要暂停下来精确计算，直接算过去。
  // 这是解决"仿真卡死"的神器。
  
  // --- 鼻轮地形 ---
  h_n = noEvent(
    if x_n < x_start_up then 0                                              // 平地接近
    else if x_n < x_end_up then (floor((x_n - x_start_up)/stepD) + 1) * stepH // 上楼梯
    else if x_n < x_start_down then H_peak                                  // 楼顶平台
    else if x_n < x_end_down then H_peak - (floor((x_n - x_start_down)/stepD) + 1) * stepH // 下楼梯
    else 0                                                                  // 终点平地
  );

  // --- 前主轮地形 ---
  h_f = noEvent(
    if x_f < x_start_up then 0
    else if x_f < x_end_up then floor((x_f - x_start_up)/stepD + 1e-3) * stepH
    else if x_f < x_start_down then H_peak
    else if x_f < x_end_down then max(0, H_peak - floor((x_f - x_start_down)/stepD + 1e-3) * stepH)
    else 0
  );

  // --- 后主轮地形 ---
  h_r = noEvent(
    if x_r < x_start_up then 0
    else if x_r < x_end_up then floor((x_r - x_start_up)/stepD + 1e-3) * stepH
    else if x_r < x_start_down then H_peak
    else if x_r < x_end_down then max(0, H_peak - floor((x_r - x_start_down)/stepD + 1e-3) * stepH)
    else 0
  );

  // =============================================================
  // C. 接触物理模型 (弹簧阻尼系统)
  // =============================================================
  // 判断鼻轮是否接触地面
  nose_on_ground = noEvent(z_n < h_n + R_nose);
  
  // 计算支持力 Fz = 刚度 * 压缩量 - 阻尼 * 速度
  // max(0, ...) 确保力不为负 (地面不能吸住轮子)
  Fz_n = if nose_on_ground then max(0, k_ground * ((h_n + R_nose) - z_n) - c_ground * vz) else 0;
  Fz_f = max(0, k_ground * ((h_f + R_main) - z_f) - c_ground * vz);
  Fz_r = max(0, k_ground * ((h_r + R_main) - z_r) - c_ground * vz);

  // =============================================================
  // D. 智能动力控制系统 (Smart Cruise Control)
  // =============================================================
  // 这个逻辑同时兼容上楼和下楼：
  // 1. 上楼时 (阻力大，速度慢): v < 0.4，power_factor = 1.0 (全油门)
  // 2. 平台时 (阻力小): v 接近 0.4，power_factor 自动减小 (维持巡航)
  // 3. 下楼时 (重力加速): v > 0.6，power_factor = -1.0 (全刹车/反拖)
  power_factor = noEvent(
      if v < target_speed then 1.0
      else if v < target_speed * 1.5 then 1.0 - (v - target_speed)/(target_speed*0.5)
      else -1.0 
  );

  // 牵引力分配 (考虑摩擦力限制)
  Fx_n = if nose_on_ground then power_factor * min(T_nose_max / R_nose, mu_s * Fz_n) else 0;
  Fx_f = power_factor * min(T_main_max / R_main, mu_s * Fz_f);
  Fx_r = power_factor * min(T_main_max / R_main, mu_s * Fz_r);

// =============================================================
  // E. 刚体动力学方程 (F=ma)
  // =============================================================
  // X轴: 驱动力 - 阻力 = 质量 * 加速度
  m * der(v) = Fx_n + Fx_f + Fx_r - 50.0 * v; 
  
  // Z轴: 支持力 - 重力 = 质量 * 垂直加速度
  m * der(vz) = Fz_n + Fz_f + Fz_r - m * g;
  
  // 俯仰角: 力矩平衡
  // 鼻轮力矩(抬头) + 前轮力矩 - 后轮力矩 - 阻尼 = 转动惯量 * 角加速度
  I_pitch * der(omega_psi) = Fz_n * L_nose_CoM + Fz_f * a - Fz_r * b - 300.0 * omega_psi; 

  // 积分计算位置
  der(x) = v * cos(psi) - vz * sin(psi);
  der(z) = vz;
  der(psi) = omega_psi;

  // =============================================================
  // F. 仿真器配置 (极速模式)
  // =============================================================
  annotation (
    experiment(
        StartTime=0, 
        StopTime=120,    // 延长到120秒，确保跑完上下楼全程
        Tolerance=1e-2,  // 放宽公差，允许1%的误差，换取极快的计算速度
        Interval=0.05,   // 每0.05秒记录一次数据 (足够画图了)
        // 强制使用 Euler 求解器，这是处理下楼撞击最快的方法
        __OpenModelica_simulationFlags(s="euler", w=true)
    )
  );
end ZLTECH_Full_Mission;
