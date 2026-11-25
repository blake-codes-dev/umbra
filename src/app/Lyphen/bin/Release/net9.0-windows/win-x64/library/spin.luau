-- name: spin
-- desc: rotates you on the spot, load again to stop
local RunService = game:GetService("RunService")

if _G.__lyphenSpin then
    _G.__lyphenSpin:Disconnect()
    _G.__lyphenSpin = nil
else
    _G.__lyphenSpin = RunService.RenderStepped:Connect(function(dt)
        local char = game:GetService("Players").LocalPlayer.Character
        local root = char and char:FindFirstChild("HumanoidRootPart")
        if root then root.CFrame = root.CFrame * CFrame.Angles(0, dt * 6, 0) end
    end)
end
