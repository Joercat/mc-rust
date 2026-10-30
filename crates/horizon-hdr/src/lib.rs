pub mod wayland {
    #[derive(Clone, Copy, Debug, PartialEq)]
    pub struct Luminances {
        pub reference: f32,
        pub target_min: f32,
        pub target_max: f32,
    }
    pub struct Watch;
    impl Watch {
        pub fn new(_window: &impl Sized) -> Option<(Self, Option<Luminances>)> { None }
        pub fn changed(&mut self) -> Option<Option<Luminances>> { None }
    }
}

pub fn open_vulkan_device(
    _adapter: &wgpu::Adapter,
    _desc: &wgpu::DeviceDescriptor<'_>,
) -> Option<Result<(wgpu::Device, wgpu::Queue), wgpu::RequestDeviceError>> {
    None
}

pub fn set_metadata(
    _device: &wgpu::Device,
    _surface: &wgpu::Surface<'_>,
    _paper_white: f32,
    _peak: f32,
    _black: f32,
) {}
