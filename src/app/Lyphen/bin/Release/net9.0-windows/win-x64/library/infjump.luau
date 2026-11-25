-- name: infinite jump
-- desc: jump in mid air, unlimited. load again to turn off
local Players = game:GetService("Players")
local UserInputService = game:GetService("UserInputService")

if _G.__lyphenInfJump then
    _G.__lyphenInfJump:Disconnect()
    _G.__lyphenInfJump = nil
else
    _G.__lyphenInfJump = UserInputService.JumpRequest:Connect(function()
        local char = Players.LocalPlayer.Character
        local humanoid = char and char:FindFirstChildOfClass("Humanoid")
        if humanoid then humanoid:ChangeState(Enum.HumanoidStateType.Jumping) end
    end)
end
