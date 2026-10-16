# V11PhysicalB1

`V11PhysicalB1` 是冻结协议 `v11-s3-b-physics-protocol-1` 的 Modelica 静态实现。它依赖本机选定的 `Modelica 4.0.0+maint.om`，直接组合 MSL 的 `World`、`Body`、`FixedTranslation`、`Planar`、`Revolute`、`WorldForceAndTorque` 和 `Rotational.Sources.Torque(useSupport=true)`。本目录没有执行 `loadFile`、`checkModel`、`buildModel` 或仿真，因此不能据此声称编译通过、数值可信或物理通过。

包内共享层如下：

- `RigidBody2D`：将协议的质心质量和面外惯量映射到真实 `MultiBody.Parts.Body`；`I_22=J`，其余两个主惯量为 `J/2`。
- `PolylineWheelContact`：统一实现有向有限折线、起点包含/终点排除的半开面段、凸角唯一径向特征和凹角双面接触。面特征 ID 为 `100+segment_index`，凸角 ID 为 `200+vertex_index`。它从覆盖轮心 x 的非竖直轮廓段插值实体顶面；轮心进入实体时立即断言失败，不会把深穿透静默改写成离地零力。
- `WheelPair`：左右同步轮对的单次等效质量、惯量、刚度和阻尼，真实轮速来自 `Revolute(n={0,-1,0})` 与轮体绝对角速度；没有由车速预设轮速。
- `IdealDriveSupply`：成对理想扭矩、额定限制、无再生定压功率和能量账本。扭矩源输入按协议使用 `-torqueCommand`。
- `PlanarWheelFixture`：允许 x/z 平移的无质量载体；开启 `Planar.revolute.useAxisFlange`，连接 `Rotational.Components.Fixed` 锁定俯仰。`supportTorque` 取该真实约束施于载体的反力矩，正方向为世界 −y，因此与正向滚动驱动扭矩相加应为 0。轴承施于载体的反作用与此约束力矩相反。

六个可构建模型为：

- `V11PhysicalB1.B01PassiveJoint`
- `V11PhysicalB1.B02DropStatic`
- `V11PhysicalB1.B03DriveBrake`
- `V11PhysicalB1.B04RiserImpact`
- `V11PhysicalB1.B05CornerImpact`
- `V11PhysicalB1.B06RollOff`

每个模型使用 Modelica 内建独立变量 `time`，并输出 `x,z,vx,vz,omega,Fx,Fz,normal,penetration,slip,normalLoss,frictionLoss,elasticEnergy,kineticEnergy,potentialEnergy,shaftPower,dcPower,driveLoss,energyAvailable,driveTorque,supportTorque,contactCount,featureId`。协议字段 `normalLoss`、`frictionLoss` 和 `driveLoss` 都是瞬时耗散功率，供独立验证器按原始时间列积分；兼容别名 `normalLossPower`、`frictionLossPower`、`driveLossPower` 数值相同。另提供从 0 开始的累计量 `normalDissipatedEnergy`、`frictionDissipatedEnergy`、`driveDissipatedEnergy`，但它们不替代独立积分证据。`slip` 和 `featureId` 对应压入量最大的当前特征，`normal` 是所有有效特征单边法向力标量之和。

不适用量明确置 0。B01 另外输出 `theta,jointOmega,alpha,pivotFx,pivotFz,jointTorque,fixedPositionResidual,fixedAngleResidual`；其中两个固定残差分别从实际连接的 MSL frame 位置和方向计算，`jointTorque` 取真实 `Revolute.tau`。由于公共字段已经占用 `omega`，B01 的关节角速度专名为 `jointOmega`，同时公共 `omega=jointOmega`。

符号约定：世界平面为 x-z，重力沿 -z；`Planar.n={0,-1,0}`。项目正 pitch 对应世界 -y。轮向 +x 纯滚时 `omega>0` 表示绕世界 +y，故轴承相对滚速为 `-bearing.w`。接触切向 `t=(nz,-nx)`，`slip=v_center·t-R*omega`，轮体世界 y 接触力矩为 `-R*Ft`。`supportTorque` 正方向沿 `Planar.n`，即世界 -y。

接触法向规律是冻结协议指定、与 MSL 4.0.0 `ElastoGap` 的 n=1 有界阻尼形式等价的薄适配：`Fe=k*delta`，`Fd=clamp(c*delta_dot,-Fe,+Fe)`，`Fn=Fe+Fd`，无接触时严格为 0。MSL 由 Modelica Association 以 BSD-3-Clause 许可发布；本包只依赖安装库并未复制其完整源文件。实现与协议都没有把该弹性近似宣称为标定轮胎模型。

当前未验证风险包括：Modelica 翻译器是否接受所有数组算法和连接平衡、真实工具链的状态选择、非光滑特征切换的事件序列、守恒误差和全部协议门槛。这些风险必须由主窗口按已批准且有账本的独立运行批次验证，不能用本 README 代替。
