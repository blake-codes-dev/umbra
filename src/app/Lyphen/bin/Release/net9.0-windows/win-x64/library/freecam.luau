-- name: free cam
-- desc: detaches the camera from your body. WASD to fly, load again to attach
local Workspace = game:GetService("Workspace")
local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")

if _G.__lyphenFreecam then
    _G.__lyphenFreecam:Disconnect()
    _G.__lyphenFreecam = nil
    Workspace.CurrentCamera.CameraType = Enum.CameraType.Custom
else
    local camera = Workspace.CurrentCamera
    local speed = 60
    camera.CameraType = Enum.CameraType.Scriptable
    _G.__lyphenFreecam = RunService.RenderStepped:Connect(function(dt)
        local direction = Vector3.zero
        if UserInputService:IsKeyDown(Enum.KeyCode.W) then direction += camera.CFrame.LookVector end
        if UserInputService:IsKeyDown(Enum.KeyCode.S) then direction -= camera.CFrame.LookVector end
        if UserInputService:IsKeyDown(Enum.KeyCode.A) then direction -= camera.CFrame.RightVector end
        if UserInputService:IsKeyDown(Enum.KeyCode.D) then direction += camera.CFrame.RightVector end
        if direction.Magnitude > 0 then
            camera.CFrame = camera.CFrame + direction.Unit * speed * dt
        end
    end)
end
